"""The signal that came before the kill, with the sender's name on it.

SIGKILL cannot be witnessed by the process it ends. What almost always can be
is the SIGTERM that came first: ``docker stop``, a kubelet eviction, ``systemd
stop``, ``timeout(1)``, GitLab's and Jenkins' cancellation, ``earlyoom`` - all
of them ask before they kill, and the asking is an ordinary catchable signal
whose ``siginfo`` names the sender. A SIGKILL that lands ten seconds after
"SIGTERM from ``Runner.Listener`` pid 812" is explained; the same SIGKILL on
its own is a guess.

Python's ``signal.signal`` handler is handed no siginfo, so the sender is read
another way: the signal is *blocked* in every thread and one thread waits for
it with ``sigtimedwait``, which returns the ``siginfo`` - pid, uid and
``si_code`` - and consumes the signal. The waiting thread writes that down,
reads the sender's comm and command line out of ``/proc`` while the sender
still exists, and then re-raises the signal with its default disposition so
the process dies exactly as it would have. Nothing about the run's outcome
changes; one line on disk does.

Blocking has to happen before any other thread exists, because a thread
created later inherits the mask and one created earlier does not - and a
process-directed signal is delivered to any thread that has it unblocked.
That is why it happens at ``pytest_configure`` in the controller, before xdist
spawns its gateways. It is also why this is a *controller* facility: children
inherit the mask too, and a test's own subprocesses must not be born deaf to
SIGTERM. The controller runs no tests. Its children are the workers, and each
of them unblocks at its own start - see :func:`unblock_inherited`, called from
``registration.install`` before anything else a worker does.

Only a signal whose disposition is the default is taken. A handler somebody
installed means they wanted the signal, and stealing it would be worse than
not knowing who sent it.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from typing import Any, Callable, Optional

#: Spelled here rather than imported from ``probes.platform_flags``, on
#: purpose: importing anything under ``probes`` runs its ``__init__``, which
#: imports psutil at module scope - and this module is imported by
#: ``registration.install`` in every worker, *before* the guard that turns a
#: psutil that will not import into a warning rather than an INTERNALERROR.
#: See the registration module docstring for why that placement is the point.
IS_LINUX = sys.platform.startswith("linux")

#: What the controller witnesses. SIGTERM is the one every orchestrator sends
#: before SIGKILL, and the one whose sender explains the kill that follows.
WITNESSED: tuple[int, ...] = tuple(
    number for number in (getattr(signal, "SIGTERM", None),) if number is not None
)

#: The event a witnessed signal is recorded as.
EVENT = "signal_received"

SI_USER = 0
SI_KERNEL = 128


def supported() -> bool:
    return IS_LINUX and hasattr(signal, "sigtimedwait") and hasattr(signal, "pthread_sigmask")


def block(signals: tuple[int, ...] = WITNESSED) -> set[int]:
    """Block the signals nobody has claimed, in the calling thread.

    Returns the set actually blocked. Empty where unsupported, and a signal
    with a handler installed is left alone - see the module docstring.
    """
    if not supported():
        return set()
    wanted = {number for number in signals if signal.getsignal(number) == signal.SIG_DFL}
    if not wanted:
        return set()
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, wanted)
    except (OSError, ValueError):
        return set()
    # A fork()ed child inherits the mask - multiprocessing's default start
    # method on Linux, a test's os.fork - and must not be born deaf to
    # SIGTERM. Undone in the child, at the one point Python offers for it.
    # (subprocess children are exec()ed and skip these hooks; a worker undoes
    # the block itself - see unblock_inherited.)
    register = getattr(os, "register_at_fork", None)
    if register is not None:
        try:
            register(after_in_child=lambda: unblock(set(wanted)))
        except (OSError, ValueError, TypeError):
            pass
    return wanted


def unblock(signals: set[int]) -> None:
    if not signals or not supported():
        return
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, signals)
    except (OSError, ValueError):
        pass


def unblock_inherited(signals: tuple[int, ...] = WITNESSED) -> None:
    """For a worker: undo the mask it was born with.

    A worker is spawned by a controller that has SIGTERM blocked, and inherits
    that. Left blocked, a ``docker stop`` would never reach the worker and it
    would die by the SIGKILL that follows the grace period instead, taking a
    test's own subprocesses' inheritance with it. Called before the recorder
    is built, so it happens whether or not the recorder can be - and it must
    never raise, for the same reason: it runs inside ``pytest_configure``.
    """
    try:
        unblock(set(signals))
    except Exception:  # noqa: BLE001 - bookkeeping must never break a run
        pass


class SignalWitness:
    """Waits for the blocked signals, records each one, then lets it through."""

    def __init__(
        self,
        record: Callable[..., None],
        signals: set[int],
        poll_seconds: float = 0.5,
    ) -> None:
        self.record = record
        self.signals = signals
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: Set if the waiting thread gave up. A blocked signal with nobody
        #: waiting for it is a run that cannot be stopped, so whoever owns
        #: the block checks this and releases it - see the engine.
        self.failed = False

    def start(self) -> None:
        if not self.signals or not supported():
            return
        self._thread = threading.Thread(
            target=self._wait, name="failure-instrumentation-signals", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_seconds * 3)

    def _wait(self) -> None:
        refusals = 0
        while not self._stop.is_set():
            try:
                info = signal.sigtimedwait(self.signals, self.poll_seconds)
            except (OSError, ValueError):
                # EINTR is ordinary and retried; anything persistent means
                # this thread cannot do its job, and it says so rather than
                # leaving the signal blocked with nobody listening.
                refusals += 1
                if refusals >= 3:
                    self.failed = True
                    return
                self._stop.wait(0.2)
                continue
            refusals = 0  # they have to be consecutive to mean anything
            if info is None:
                continue
            self._witness(info)
            self._let_through(info.si_signo)
            # Still here, so the delivery did not end the process: a handler
            # somebody installed after the block took it. Keep waiting rather
            # than returning. The block is process-wide, so a witness that
            # stops after one signal leaves the next stop request pending
            # with nobody to receive it - undeliverable, unwitnessed, and a
            # run that can no longer be stopped.

    def _witness(self, info: Any) -> None:
        sender = int(info.si_pid)
        try:
            self.record(
                EVENT,
                signal=int(info.si_signo),
                name=_name(int(info.si_signo)),
                si_code=int(info.si_code),
                origin=origin_of(int(info.si_code), sender),
                sender_pid=sender,
                sender_uid=int(info.si_uid),
                sender_comm=_proc(sender, "comm"),
                sender_cmdline=_proc(sender, "cmdline"),
            )
        except Exception:  # noqa: BLE001 - a record that cannot be written must not
            pass  # stop the signal being let through below

    def _let_through(self, number: int) -> None:
        """Re-raise with whatever disposition is in force now.

        The signal was consumed by ``sigtimedwait``. Unblocking it in this
        thread and raising it here delivers it to this thread, and the default
        action for SIGTERM ends the whole process - which is what would have
        happened without us. A handler somebody installed since the block is
        honoured by the same delivery.

        Returning from this means the process survived the delivery, and the
        block goes straight back on: it is unblocked in this thread only, so
        leaving it off would have the *next* one delivered here instead of
        waited for, and the witness would miss the signal it exists to name.
        """
        try:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {number})
            try:
                signal.raise_signal(number)
            finally:
                signal.pthread_sigmask(signal.SIG_BLOCK, {number})
        except (OSError, ValueError):
            os.kill(os.getpid(), number)


def origin_of(si_code: int, sender_pid: int) -> str:
    """Who a signal came from, in one word a reader can act on."""
    if si_code == SI_KERNEL:
        return "kernel"
    if sender_pid == os.getpid():
        return "self"
    if si_code >= 0:
        return "process"
    return "process"  # SI_QUEUE, SI_TKILL: still a userspace sender


def _proc(pid: int, name: str) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/{name}", "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    text = raw.decode("utf-8", "replace").replace("\0", " ").strip()
    return text or None


def _name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"
