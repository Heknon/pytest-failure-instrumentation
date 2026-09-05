"""Every setting in one place, resolved once.

Defaults are chosen so that installing the plugin and doing nothing costs a
run almost nothing: the watchdog samples on a timer rather than per test, the
expensive probes are off, and no memory ceiling is imposed.

There are two ways settings arrive. Most runs read them from ini, which is
what ``resolve`` does. A framework that wraps pytest usually cannot: its
values come from its own configuration, computed in Python, and it wants them
applied without asking every team to copy an ini block. Those runs build a
``Settings`` and hand it to :func:`.install`. Both paths end in the same
frozen object, and the invariants below are enforced on the object rather than
in ``resolve``, so a hand-built one cannot skip them.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Optional

import pytest

DEFAULT_DIRECTORY = ".pytest-failures"

#: What a run with no workers records itself under. xdist names its workers
#: ``gw0`` upwards and everything downstream - the evidence files, the live
#: view, a sample row - is keyed on that name, so a run that has no workers
#: still needs one to be the process running the tests. It is the session
#: itself, and this is what it is called. Already the name the run summary
#: reports for a single-process run, which is where it comes from.
SOLE_WORKER = "main"

#: "Pick one for me" - the value the kernel itself reads as *any free port*,
#: so the lottery costs no branch at the bind. It is the default because a
#: session that has not been told which port to use has no reason to fight the
#: other sessions on the host for one: it serves its own workers on a port
#: nobody else wants, and writes down where. A port is only worth fixing when
#: something outside has to be told about it once - a firewall rule, a UI with
#: the address compiled in - and that is what naming one means.
PORT_LOTTERY = 0

#: The conventional fixed port, used when one is asked for by name rather than
#: by number. Not a default: 8080 is the most contended port on a developer's
#: machine, and taking it uninvited is how this feature turns itself off.
CONVENTIONAL_STACK_SERVER_PORT = 8080

#: Loopback. Overridable, because a container's UI is not inside the container
#: - see ``failure_stack_server_host``.
DEFAULT_STACK_SERVER_HOST = "127.0.0.1"

#: Addresses that reach this machine and nowhere else. ``localhost`` is in the
#: list because people type it, and it resolves to one of the other two.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

#: Where the live server's token comes from when it is not on the command
#: line. An environment variable because that is how a secret reaches a
#: container, a CI job and a shell, and because it leaves no file behind - see
#: :func:`_add_command_line_options` for why there is deliberately no ini.
TOKEN_ENV = "PYTEST_CALLSTACK_TOKEN"

#: The settings whose *value* is a credential, so anything that prints a
#: setting prints the name and stops there - see
#: :func:`.registration._difference`, which is where one of these reached a
#: warnings summary and a CI log.
#:
#: A set rather than the one ``if name == "stack_server_token"`` it would take
#: today, because the cost of the two spellings is the same and only one of
#: them keeps holding: a second secret field added later is redacted by
#: appearing here, rather than by whoever adds it remembering every place a
#: setting gets formatted.
SECRET_SETTINGS = frozenset({"stack_server_token"})

#: Below this the heartbeat thread costs more than it measures. Clamped on the
#: object rather than where a setting is read, because the controller decides
#: what counts as a stale beat from the same number - and two different
#: answers to "how often does a worker beat" make a healthy worker look frozen.
MIN_HEARTBEAT_INTERVAL = 1.0

#: The sampler walks a directory, reads every state file and tails every
#: event log on each pass. A cadence below this is a busy loop wearing a
#: setting's clothes.
MIN_SAMPLE_SECONDS = 1.0

#: Accepted values for ``failure_tracer``. Anything else falls back to the
#: default rather than failing a run over a typo in an ini file - and says so,
#: which is the part this had missing: see :meth:`Settings._usable_tracer`.
TRACER_POLICIES = ("parent", "any", "off")


class FailureInstrumentationWarning(UserWarning):
    """Raised for a setting this plugin could not use, and for setup it had to
    give up on. Never an error: nothing here is worth ending a run over."""


def advise(message: str) -> None:
    """Say the above, without being able to end the run - see that promise.

    Every warning in this module is raised from ``pytest_configure``: either
    building :class:`Settings` or reading an ini value on the way there. A
    project running ``filterwarnings = error``, which is a recommended setting
    and a common one, turns each of them into an exception thrown out of a
    configure hook, and pytest has one answer for that - INTERNALERROR, exit
    status 3, not a single test collected. "Never an error" was a promise this
    module made and could not keep.

    Measured: ``--callstack-host 0.0.0.0`` - the documented way to reach the
    live view from outside a container, which warns on purpose because it is a
    real exposure - ended the whole session that way; so did
    ``failure_stall_seconds = 5m``. A plugin installed to report failures does
    not get to be the thing that stops the run, least of all over advice about
    its own configuration.

    The advice is downgraded rather than dropped: re-emitted with the
    project's filters set aside, which leaves it in the warnings summary where
    it was always meant to be read. pytest's own recorder is left in place, so
    it is reported exactly as before on every project that had not turned
    warnings into errors.
    """
    try:
        warnings.warn(message, FailureInstrumentationWarning, stacklevel=3)
    except FailureInstrumentationWarning:
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.warn(message, FailureInstrumentationWarning, stacklevel=3)


@dataclass(frozen=True)
class Settings:
    """Everything this plugin reads, and the only thing :func:`.install` takes.

    Every field has a default, so a caller states what it cares about and
    leaves the rest alone::

        Settings(packages=("yourcore",), directory=Path("/var/log/evidence"))

    ``__post_init__`` coerces and clamps, because this is a public type now: a
    framework passing ``packages=["a", "b"]`` or ``directory="/tmp/x"`` means
    the obvious thing, and a heartbeat interval below the floor is the bug the
    floor exists to prevent whichever way the object was built.
    """

    directory: Path = Path(DEFAULT_DIRECTORY)
    #: Your own top-level packages, for attribution.
    packages: tuple[str, ...] = ()
    product_version: Optional[str] = None
    watchdog: bool = True
    heartbeat_interval: float = 5.0
    tracemalloc_depth: int = 0
    object_census: bool = False
    high_water_mb: int = 0
    memory_limit_mb: int = 0
    #: How long a test may run before it starts leaving a stack, and how often
    #: it refreshes it after that - ``repeat=True``, so this is also the
    #: staleness bound on the only stack a stalled worker has on Windows. The
    #: file is emptied when the test ends, so the cadence costs one test's
    #: worth of dumps rather than a run's.
    slow_test_seconds: float = 20.0
    stall_seconds: float = 300.0
    stack_probe: bool = True
    #: Whether a run with **no workers** keeps its own fatal-signal dump.
    #:
    #: A worker always keeps one and gives up nothing for it: the stderr the
    #: dump would otherwise go to is shared with every other worker, so a
    #: crash written there is interleaved with fifteen streams and read by
    #: nobody. A run with no workers is the opposite case. Its stderr is a
    #: terminal, or a CI log somebody keeps, and pytest's own faulthandler
    #: plugin is already writing the crash there - so claiming it moves the
    #: only account of the crash that run will produce *while it is happening*
    #: into a file, to be reported when a later run reads it.
    #:
    #: There is no having both: ``faulthandler`` keeps one destination for a
    #: fatal signal and CPython refuses to register a second. See
    #: :func:`..capture.crash_stack.arm_fatal_handler`.
    #:
    #: So it is off, and what that costs is the stack rather than the report.
    #: A death recovered from such a run still names the test in flight, its
    #: phase, the counters and the memory, and still carries a
    #: ``suspect_owner`` read from the test's own module; what it cannot carry
    #: is a blamed frame. Turn it on where the incident is the artefact that
    #: gets read and the terminal is not, which is most of CI.
    crash_stack: bool = False
    #: Whether this run witnesses who signals its processes - see
    #: :mod:`.incidents.killer`. Kernel tracing uses Linux tracefs or Windows
    #: ETW where permitted; it never changes signal masks or handlers.
    kill_trace: bool = True
    #: Whether this run may spend ``sudo -n`` on the sources that need root:
    #: the kernel log where ``/dev/kmsg`` and the journal are closed to this
    #: user, and the tracepoint above. Off by default, because a plugin that
    #: starts running sudo on everybody who upgrades it has overstepped;
    #: ``-n`` means a sudo that would prompt fails instead, so an unattended
    #: run is never left waiting for a password.
    elevate: bool = False
    #: Keep a bounded copy of each worker's stderr, so the one line that
    #: explains a native death - ``pthread_create failed``, ``malloc():
    #: corrupted top size``, an abort message - survives the kill that
    #: swallows it. It points fd 2 at a real file for the length of each phase,
    #: so a write followed at once by ``abort()`` is on disk before the abort
    #: runs, and hands the bytes back to pytest's own capture at the phase's
    #: end so nothing pytest would have shown is lost. Off by default and
    #: POSIX-only: it is the single facility here that takes over a
    #: process-wide file descriptor, so it is opt-in and every step is guarded
    #: against ending a run. See :mod:`.capture.output`.
    capture_output: bool = False
    #: What to call when this run is killed - see :mod:`.incidents.reporter`.
    #: A run whose controller dies has nobody left to raise its incidents,
    #: and on a runner with a fresh workspace per job there is no next run
    #: to recover them; the sidecar that outlives the controller reports them
    #: instead, by calling this with each incident the way the hook would be.
    #:
    #: It travels to a different process as a pickle, which sets the rules: a
    #: module-level function, or a ``functools.partial`` of one with picklable
    #: bound arguments, or a dotted path ``package.module:attribute`` (the
    #: only form ini can express). A lambda or a closure is warned about at
    #: session start and the run proceeds without a reporter. It runs as the
    #: user that started the run, with that user's environment, never as
    #: root - the sidecar itself unpickles nothing.
    on_run_death: Any = None
    #: Which declaration a worker makes on Linux, where Yama restricts who may
    #: read a process. "parent" nominates the controller and covers a session
    #: reading its own workers, which is how the live view is used by default;
    #: "any" is what a *shared* server needs, since another session's reader is
    #: no descendant of this controller; "off" declares nothing. Nothing
    #: outside Linux consults it.
    #:
    #: This says *which* declaration a run would make, not that one is made:
    #: whether anything is declared at all is :attr:`tracer_in_force`.
    tracer: str = "parent"
    #: The answer :attr:`tracer_in_force` was given by the controller, or None
    #: where nothing has been handed down and this process must work it out
    #: for itself. Only ever set by :meth:`as_payload`, which is the one place
    #: that knows both halves of the question.
    tracer_handed_down: Optional[str] = None
    #: How often to push a ``pytest_failure_worker_sample`` while the run is
    #: going. 0 is off, and is the default: this is the only hook here that
    #: fires when nothing is wrong, so it is the only one a run pays for
    #: continuously - see :mod:`.sampling`.
    sample_seconds: float = 0.0
    #: Serve the stack of any local process over HTTP, for a UI watching a run
    #: - see :mod:`.stack_server`. Off by default: opening a listening socket
    #: is not something a plugin installed for crash reporting should start
    #: doing to everybody who upgrades it.
    stack_server: bool = False
    #: 0 draws a free port and writes it down; any other number is claimed,
    #: shared with the other sessions on the host, and waited for if somebody
    #: else has it first. See :mod:`.stack_server` for why those are different
    #: modes rather than one with a different number in it.
    stack_server_port: int = PORT_LOTTERY
    #: What the server binds. Loopback keeps it off the network, which is
    #: right on a laptop and wrong in a container, where the UI is outside and
    #: 127.0.0.1 is unreachable from there. Anything else needs a token and is
    #: refused without one - see refuses_to_bind_unauthenticated. With one it
    #: is the deliberate exposure a container needs, and nothing is said about
    #: it: the advice that used to restate the bind on every run was telling
    #: the person who typed the address what they had typed, in the one place
    #: it is typed for, at every start of every job.
    stack_server_host: str = DEFAULT_STACK_SERVER_HOST
    #: Whether the live server may answer ``/stack?locals`` with each frame's
    #: variables.
    #:
    #: On, because the variables are most of what makes a live stack worth
    #: reading: "waiting on a lease" and "waiting on a lease it has already
    #: waited 27 seconds for" are the same frame and different bugs, and a
    #: reader who has to know to ask is a reader who mostly does not.
    #:
    #: It is nonetheless the one thing this server discloses that is *data*
    #: rather than the shape of the code - a fixture's credentials, a customer
    #: record, a decrypted payload, whatever the failing test happens to be
    #: holding - so a deployment that cannot have that leave the process turns
    #: it off here and still gets the frames. Nothing is redacted selectively:
    #: this package cannot tell a password from a lease id, and a filter that
    #: pretended to would be worse than the honest switch.
    #:
    #: The variables are rendered by py-spy inside its own process while the
    #: target is stopped, so what crosses the wire is text: no ``__repr__`` of
    #: yours is executed to produce it.
    stack_server_locals: bool = True
    #: What every endpoint but ``/identity`` demands, or "" for a server that
    #: asks nothing. Supplied rather than minted, and never written down by
    #: this package: the address of a drawn port has to be published, but the
    #: secret guarding it is the one value both ends can agree on in advance,
    #: so making it discoverable is a cost with nothing on the other side.
    stack_server_token: str = ""
    #: How many workers share the machine. Per worker, never sent between
    #: processes - the controller's copy would be wrong on every worker.
    worker_count: int = 1
    #: Which run this is. Minted by the controller and pushed down; likewise
    #: never copied from one process's settings to another's.
    run_id: Optional[str] = None
    #: Sample every thread's stack and CPU for the whole run, and raise what
    #: crosses the thresholds below as ``cpu_hotspot`` and ``memory_profile``
    #: incidents - see :mod:`.profile`. Off: it is the one thing here with a
    #: continuous running cost on a healthy run, bounded by the profiler's
    #: serial and xdist performance gates.
    profile: bool = False
    #: Seconds between samples.
    profile_interval: float = 0.02
    #: Percent of the run's CPU a function must hold to be raised.
    profile_cpu_share: float = 5.0
    #: Seconds of CPU a function must use before its share can be raised.
    #: Prevents noise on tiny runs while remaining tunable for short profiles.
    profile_cpu_floor_seconds: float = 0.5
    #: Megabytes a test may keep, or climb by, before it is raised.
    profile_retained_mb: int = 100
    #: Resident megabytes no test may reach, whatever it started from. 0 is
    #: off. The retained threshold catches a climb; this catches a size.
    profile_peak_mb: int = 0
    #: Trace allocations with tracemalloc as well, so a memory finding names
    #: the lines holding the memory and a memory flame graph is written.
    #: Several times slower on allocation-heavy code: for a rerun of the
    #: tests the untraced run named, never the nightly.
    profile_allocations: bool = False
    #: Frames tracemalloc keeps per allocation when tracing. Deeper reaches
    #: further up the stack and costs more per allocation.
    profile_allocation_depth: int = 12
    #: Cores the process must burn, over consecutive tenth-second windows,
    #: for a stretch to be a burst.
    profile_burst_cores: float = 0.7
    #: Seconds a burst must last to be raised on its own. Shorter bursts
    #: are raised only when the same code bursts across many tests.
    profile_burst_seconds: float = 2.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "directory", Path(self.directory))
        object.__setattr__(self, "packages", tuple(self.packages))
        object.__setattr__(
            self,
            "heartbeat_interval",
            max(MIN_HEARTBEAT_INTERVAL, float(self.heartbeat_interval)),
        )
        object.__setattr__(
            self,
            "sample_seconds",
            0.0 if self.sample_seconds <= 0 else max(MIN_SAMPLE_SECONDS, float(self.sample_seconds)),
        )
        object.__setattr__(self, "tracer", self._usable_tracer())
        object.__setattr__(
            self,
            "tracer_handed_down",
            self.tracer_handed_down if self.tracer_handed_down in TRACER_POLICIES else None,
        )
        object.__setattr__(self, "stack_server_host", str(self.stack_server_host))
        object.__setattr__(
            self, "stack_server_token", str(self.stack_server_token or "").strip()
        )
        object.__setattr__(self, "stack_server_port", int(self.stack_server_port))
        object.__setattr__(self, "profile", bool(self.profile))
        object.__setattr__(
            self, "profile_interval", max(MIN_PROFILE_INTERVAL, float(self.profile_interval))
        )
        object.__setattr__(self, "profile_cpu_share", max(0.0, float(self.profile_cpu_share)))
        object.__setattr__(
            self, "profile_cpu_floor_seconds", max(0.0, float(self.profile_cpu_floor_seconds))
        )
        object.__setattr__(self, "profile_retained_mb", max(1, int(self.profile_retained_mb)))
        object.__setattr__(self, "profile_peak_mb", max(0, int(self.profile_peak_mb)))
        object.__setattr__(self, "profile_allocations", bool(self.profile_allocations))
        object.__setattr__(self, "profile_allocation_depth", max(1, int(self.profile_allocation_depth)))
        object.__setattr__(self, "profile_burst_cores", max(0.05, float(self.profile_burst_cores)))
        object.__setattr__(self, "profile_burst_seconds", max(0.1, float(self.profile_burst_seconds)))
        self._warn_if_a_stall_is_judged_before_it_has_evidence()
        self._warn_if_the_port_is_not_a_port()

    def _usable_tracer(self) -> str:
        """The policy as written, or the default with the reason said out loud.

        Still a fallback and not an error: a typo in an ini file is not worth
        ending a run over, which is the rule the whole module keeps. What it
        was missing is the sentence. Every other unusable value here goes
        through :func:`advise`; this one coerced in silence, and it is the one
        setting where coercing in silence hands out a permission nobody asked
        for - ``failure_tracer = none`` is what somebody reaches for when they
        want no ptrace declaration made, it is not one of the three words, and
        it resolved to "parent". They believed they had withheld the
        permission and had granted it.

        Case and surrounding space are not typos and are not reported as
        such - ``resolve`` already folds them for the ini path, and a
        framework passing "OFF" to :func:`.install` means the same thing it
        would have meant in an ini file.
        """
        policy = str(self.tracer).strip().lower()
        if policy in TRACER_POLICIES:
            return policy
        advise(
            f"failure_tracer={self.tracer!r} is not one of "
            f"{', '.join(TRACER_POLICIES)}, so it falls back to 'parent' - which "
            "grants the permission rather than withholding it. 'off' is how a "
            "worker is told to declare nothing",
        )
        return "parent"

    def _warn_if_a_stall_is_judged_before_it_has_evidence(self) -> None:
        """The watchdog has to have fired before the stall is assessed.

        These two settings look independent and are not: the stack a stalled
        worker is reported with is whatever the watchdog last wrote, so a stall
        judged sooner than the watchdog's first dump is judged with no stack at
        all. On Windows, where nothing can ask a live process for one, that is
        every stall. Nothing is clamped - both numbers are legitimate choices -
        but silently losing the stack is the kind of absence this plugin exists
        to stop people misreading.
        """
        if not self.watchdog or self.stall_seconds <= 0 or self.slow_test_seconds <= 0:
            return  # one of the two is switched off, so there is no ordering
        if self.slow_test_seconds < self.stall_seconds:
            return
        advise(
            f"failure_slow_test_seconds ({self.slow_test_seconds:g}) is not below "
            f"failure_stall_seconds ({self.stall_seconds:g}), so a stalled worker "
            "is assessed before its watchdog has written a stack and will be "
            "reported without one",
        )

    def _warn_if_the_port_is_not_a_port(self) -> None:
        """Said here, where the number was typed, rather than only on the wire.

        A port outside 0-65535 cannot be bound, and the bind says so in an
        ``OverflowError`` rather than the ``OSError`` every other unbindable
        address answers with. The server treats it as the bind refusal it is
        and reports an incident - but an incident goes to a hook, and the
        person who mistyped a number on the command line is owed the answer in
        the terminal they typed it in.

        Not clamped, and not quietly replaced with a drawn port. Somebody who
        names a port has told something outside this run where to look; moving
        it and saying nothing leaves them watching an address the server was
        never on, which is the failure this warning exists to prevent rather
        than a fix for it.
        """
        if not self.stack_server or 0 <= self.stack_server_port <= 65535:
            return
        advise(
            f"failure_stack_server_port is {self.stack_server_port}, which is "
            "not a port - the range is 0-65535, where 0 means draw a free one. "
            "The live stack server cannot bind this and will report itself "
            "unavailable; the run is otherwise unaffected",
        )

    @property
    def tracer_in_force(self) -> str:
        """The policy this process actually declares, which is usually none.

        "off" unless something in this run reads a worker's stack while it is
        still running, and there is exactly one thing that does: the live
        stack server, answering ``/stack?pid=`` when a UI asks. :attr:`tracer`
        is consulted only once that is on.

        The sampler does not qualify, and it is worth saying why it used to.
        It pushed frames for every worker it judged stuck, which needed this
        same declaration - until the judgement turned out to be ``blocked``,
        the status of any worker under 0.05 cores, so an I/O-bound suite had
        every healthy worker read on every pass. That half is gone (see
        :mod:`.sampling`); what is left reads files, asks no worker anything,
        and needs no permission from the kernel to do it.

        Off by default because the declaration is not free and was being made
        by everybody. ``ptrace_scope=1`` is the Ubuntu and Debian default, and
        every worker of every run on those machines issued
        ``prctl(PR_SET_PTRACER, <controller pid>)`` at startup - so ``pip
        install --upgrade`` followed by ``pytest -n8`` widened who may read a
        test process, for a feature the user had not switched on. Yama admits
        the nominated pid *and its descendants*, which is the run's whole
        process tree, so what was widened is not a formality either - see
        :mod:`.probes.tracing`.

        A worker cannot answer this from what it can see. It is handed none of
        the server's settings, deliberately and for reasons of its own
        (:meth:`as_payload`), so a worker asked to decide would
        answer "off" for every run with a live view on and the feature would
        be refused by the kernel on the machines it exists for. The controller
        decides once and hands down the answer, and that answer outranks
        anything this process can see for itself.
        """
        if self.tracer_handed_down is not None:
            return self.tracer_handed_down
        return self.tracer if self.stack_server else "off"

    @property
    def refuses_to_bind_unauthenticated(self) -> bool:
        """Whether this configuration asks for an open port on the network.

        Off loopback with no token is the one combination that cannot be
        anybody's intention: it serves the stack of every process on the host
        to whoever can route to it, and a stack carries file paths, function
        names and the shape of your code. A warning is the wrong instrument
        for something nobody meant to ask for - it scrolls past, and by the
        time anyone reads it the port has been open for the length of the run.

        So the server declines to bind and reports it, through the same
        machinery a port held by a stranger uses. The run is unaffected either
        way; what differs is that it fails towards no live view rather than
        towards an open one, and the remedy is one environment variable.
        """
        return (
            self.stack_server
            and self.stack_server_host not in LOOPBACK
            and not self.stack_server_token
        )

    def with_overrides(self, **overrides: Any) -> Settings:
        """A copy with some fields changed, rejecting names that do not exist.

        A silently ignored ``pacakges=`` is a framework shipping unattributed
        incidents to every one of its customers and nobody finding out.
        """
        unknown = sorted(set(overrides) - _FIELD_NAMES)
        if unknown:
            raise TypeError(
                f"unknown failure-instrumentation setting(s): {', '.join(unknown)}. "
                f"Known settings: {', '.join(sorted(_FIELD_NAMES))}"
            )
        return replace(self, **overrides) if overrides else self

    # -- crossing a process boundary --------------------------------------

    def as_payload(self) -> dict[str, Any]:
        """A form execnet can carry to a worker.

        Only primitives: execnet serialises a fixed set of builtin types and
        nothing else, so ``Path`` and the tuple both have to be flattened.
        Per-process fields are left out rather than sent and ignored - the
        controller's worker count is not any worker's worker count.
        """
        return {
            "directory": str(self.directory),
            "packages": list(self.packages),
            "product_version": self.product_version,
            "watchdog": self.watchdog,
            "heartbeat_interval": self.heartbeat_interval,
            "tracemalloc_depth": self.tracemalloc_depth,
            "object_census": self.object_census,
            "high_water_mb": self.high_water_mb,
            "memory_limit_mb": self.memory_limit_mb,
            "slow_test_seconds": self.slow_test_seconds,
            "stall_seconds": self.stall_seconds,
            "stack_probe": self.stack_probe,
            # A worker-side recording concern, so it travels: the tee is
            # installed in the worker, not the controller. kill_trace,
            # elevate and on_run_death stay out - those are the controller's,
            # which is where the witnesses and the sidecar live.
            "capture_output": self.capture_output,
            # The declaration in force, resolved here rather than there. It
            # is the worker that makes it and the controller that knows
            # whether anybody is going to read the result - the two settings
            # that decide it are absent below, so a worker left to judge for
            # itself would decline for every run with a live view on. What
            # travels is the answer, not the evidence for it.
            "tracer_handed_down": self.tracer_in_force,
            # The profiler runs where the tests run, so a worker needs the
            # switch and the cadence. The thresholds are read on the
            # controller alone and travel only so that a worker's copy of the
            # settings is the controller's copy.
            "profile": self.profile,
            "profile_interval": self.profile_interval,
            "profile_cpu_share": self.profile_cpu_share,
            "profile_cpu_floor_seconds": self.profile_cpu_floor_seconds,
            "profile_retained_mb": self.profile_retained_mb,
            "profile_peak_mb": self.profile_peak_mb,
            "profile_allocations": self.profile_allocations,
            "profile_allocation_depth": self.profile_allocation_depth,
            "profile_burst_cores": self.profile_burst_cores,
            "profile_burst_seconds": self.profile_burst_seconds,
            # crash_stack is absent, and for a reason of its own: it asks
            # whether a run with no workers should take the fatal dump off its
            # terminal, and a worker has no terminal and no choice. It always
            # keeps its own.
            #
            # Sampling is absent for the same reason as the stack server below:
            # it runs on the controller, over every worker at once, and a
            # worker that read it would sample itself and its siblings.
            # The stack server is deliberately absent. It is the controller's
            # alone - a worker that read these and started one would have every
            # worker on the host racing for the same port, which is the exact
            # collision the server exists to avoid.
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any], **per_process: Any) -> Settings:
        """The inverse, plus the fields that only this process can answer."""
        known = {name: value for name, value in payload.items() if name in _FIELD_NAMES}
        return cls(**known, **per_process)


_FIELD_NAMES = {field.name for field in fields(Settings)}


def add_options(parser: pytest.Parser) -> None:
    _add_command_line_options(parser)
    parser.addini("failure_directory", help="Where evidence is written.", default=DEFAULT_DIRECTORY)
    parser.addini(
        "failure_packages",
        help="Your own top-level packages, so a failing frame can be told from "
        "a dependency's and from the customer's own tests.",
        type="args",
        default=[],
    )
    parser.addini("failure_product_version", help="Version recorded on every incident.", default="")
    parser.addini("failure_watchdog", help="Sample memory and liveness on a timer.", default="true")
    parser.addini("failure_heartbeat_interval", help="Seconds between liveness beats.", default="5.0")
    parser.addini(
        "failure_tracemalloc_depth",
        help="Allocation traceback frames to keep. 0 disables. 1 is cheap and "
        "names the allocating line, which is what attributes an OOM kill.",
        default="0",
    )
    parser.addini(
        "failure_object_census",
        help="Count live objects at a memory high-water mark. Off by default: "
        "walking the heap on a worker near its ceiling makes things worse.",
        default="false",
    )
    parser.addini("failure_high_water_mb", help="Absolute memory mark for a snapshot.", default="0")
    parser.addini(
        "failure_memory_limit_mb",
        help="Soft address-space cap per worker (POSIX). Turns a silent OOM "
        "kill into a MemoryError attributed to the offending test.",
        default="0",
    )
    parser.addini(
        "failure_slow_test_seconds",
        help="How long a test may run before it starts leaving a stack, and "
        "how often it refreshes it after that. This is the only stack a "
        "stalled worker has on Windows, so it doubles as the age of the "
        "freshest evidence available. The heartbeat thread writes it, so it "
        "needs failure_watchdog on and cannot be finer than "
        "failure_heartbeat_interval. 0 disables.",
        default="20",
    )
    parser.addini("failure_stall_seconds", help="Silence before a stall is assessed. 0 disables.", default="300")
    parser.addini(
        "failure_crash_stack",
        help="Keep the fatal-signal stack of a run that has no workers, "
        "instead of leaving it on the stderr pytest points it at. There is one "
        "destination and it cannot be shared, so this trades the dump your "
        "terminal shows you now for one an incident can carry later. Off by "
        "default; a worker keeps its own either way, because the stderr it "
        "would use is shared and nobody reads it.",
        default="false",
    )
    parser.addini(
        "failure_capture_output",
        help="Keep the last few KB of each worker's stderr in <worker>.output, "
        "so a native crash message (pthread_create failed, a malloc abort) "
        "survives the kill that swallows it and reaches the incident - "
        "including one printed in the very phase that crashes, which pytest's "
        "own capture never reports. Off by default, POSIX only: this points "
        "file descriptor 2 at a file for each phase, the one thing here that "
        "reaches across the whole process, and hands the output back to "
        "pytest's capture so its own reports are unchanged.",
        default="false",
    )
    parser.addini(
        "failure_kill_trace",
        help="Witness who kills this run's processes using Linux tracefs or Windows ETW "
        "where permitted. Preserves signal masks and handlers. On by default.",
        default="true",
    )
    parser.addini(
        "failure_elevate",
        help="Allow sudo -n for the witnesses that need root: the kernel log "
        "(dmesg) where /dev/kmsg and the journal are closed to this user, and "
        "the signal tracepoint. Off by default. -n means a sudo that would "
        "prompt fails rather than hangs. Linux only: on Windows the ETW "
        "session needs the run itself to hold administrator rights.",
        default="false",
    )
    parser.addini(
        "failure_on_run_death",
        help="A dotted path, package.module:attribute, to a callable the "
        "sidecar calls with each incident of a run whose controller was "
        "killed - a cancelled job, an OOM kill - so a killed run is reported "
        "at once rather than by the next run over the same directory, which "
        "on a fresh-workspace runner never comes. From Python, "
        "install(config, on_run_death=functools.partial(...)) takes a "
        "callable directly. Runs as the user that started the run.",
        default="",
    )
    parser.addini(
        "failure_stack_server",
        help="Serve the live stack of any local process over HTTP, for a UI "
        "watching a run. Each session serves its own on a port drawn for it, "
        "shared with nobody, unless failure_stack_server_port names one - a "
        "named port is shared with every other session on the host. Binds "
        "loopback unless failure_stack_server_host says otherwise, and off "
        "loopback a token is required. Every process is read with py-spy, "
        "which on macOS needs root.",
        default="false",
    )
    parser.addini(
        "failure_stack_server_port",
        help="Port for that server. 0 (the default) draws a free one and writes "
        "it to the evidence directory for a UI to read; any other number is "
        "claimed and shared with every other session on the host. Overridden by "
        "--callstack-port.",
        default=str(PORT_LOTTERY),
    )
    parser.addini(
        "failure_stack_server_host",
        help="What that server binds. Loopback by default, which keeps it off "
        "the network; 0.0.0.0 is what a container needs, since its UI is "
        "outside and cannot reach 127.0.0.1 in there. Overridden by "
        "--callstack-host.",
        default=DEFAULT_STACK_SERVER_HOST,
    )
    parser.addini(
        "failure_stack_server_locals",
        help="Whether that server answers /stack?locals with each frame's "
        "variables. On by default, and the difference between 'waiting on a "
        "lease' and 'waiting on a lease for 27 seconds'. Turn it off where a "
        "test's data must not leave the process even to a reader trusted with "
        "the frames: the variables are rendered as text by py-spy, but they "
        "are the data itself, and nothing here can tell a password from a "
        "lease id.",
        default="true",
    )
    parser.addini(
        "failure_sample_seconds",
        help="Push a worker sample to pytest_failure_worker_sample this often, "
        "while the run is going. 0 disables, and is the default.",
        default="0",
    )
    parser.addini(
        "failure_tracer",
        help="Who a worker lets read its stack on Linux under Yama: parent "
        "(the controller, the default), any (needed by a shared server), off.",
        default="parent",
    )
    parser.addini(
        "failure_stack_probe",
        help="Ask an already-diagnosed stalled worker for a fresh stack "
        "(POSIX only). Can nudge a C extension blocked in a syscall.",
        default="true",
    )
    parser.addini(
        "failure_profile",
        help="Sample every thread's stack and CPU for the whole run and raise "
        "the functions that burnt the CPU and the tests that kept the memory "
        "as cpu_hotspot and memory_profile incidents. Off by default; "
        "--failure-profile switches it on for one run.",
        default="false",
    )
    parser.addini(
        "failure_profile_interval",
        help="Seconds between profile samples.",
        default="0.02",
    )
    parser.addini(
        "failure_profile_cpu_share",
        help="Percent of the run's CPU one function must hold to be raised.",
        default="5",
    )
    parser.addini(
        "failure_profile_cpu_floor_seconds",
        help="Seconds of CPU a function must use before its share can be raised. "
        "Lower this for short diagnostic runs.",
        default="0.5",
    )
    parser.addini(
        "failure_profile_retained_mb",
        help="Megabytes a test may keep, or climb by, before it is raised.",
        default="100",
    )
    parser.addini(
        "failure_profile_peak_mb",
        help="Resident megabytes no test may reach, whatever it started from; "
        "a test that does is raised with the code that was running while "
        "the memory climbed. 0 is off.",
        default="0",
    )
    parser.addini(
        "failure_profile_allocations",
        help="Trace allocations with tracemalloc while profiling, so a memory "
        "finding names the lines holding the memory and a memory flame graph "
        "is written. Several times slower on allocation-heavy code; for a "
        "rerun of the tests an untraced run named. Implies failure_profile. "
        "Requires exclusive ownership of tracemalloc. "
        "--failure-profile-allocations does the same for one run.",
        default="false",
    )
    parser.addini(
        "failure_profile_allocation_depth",
        help="Frames tracemalloc keeps per allocation when tracing.",
        default="12",
    )
    parser.addini(
        "failure_profile_burst_cores",
        help="Cores the process must burn over consecutive tenth-second windows "
        "for a stretch to count as a CPU burst.",
        default="0.7",
    )
    parser.addini(
        "failure_profile_burst_seconds",
        help="Seconds a CPU burst must last to be raised on its own.",
        default="2",
    )


def _add_command_line_options(parser: pytest.Parser) -> None:
    """The settings worth having off the ini file.

    Everything else about this plugin is a property of the project and belongs
    in ini. Three of these are switches for one run - the instrumentation, the
    profile, the allocation trace - where the ini turns them on for every run.
    The other three are properties of *where this run happens* - which port is
    free on this machine, which interface a container needs bound, which secret
    this deployment uses - and that is not something a repository can know on
    behalf of everybody running it.

    Naming the port or the host switches the server on. An option that is
    accepted, parsed, and then silently ignored because a separate ini flag was
    left at its default is the worst of the available behaviours. The token
    does *not*, because "authenticate the server I am already running" and
    "start a server" are different requests and only one of them was made.

    **The token has no ini, deliberately.** ini files live in the repository,
    and a credential in the repository is exactly the thing removing the
    minted token got rid of. It comes from the command line or from
    ``PYTEST_CALLSTACK_TOKEN``, which is how a secret reaches a container, a
    CI job and a shell - and neither leaves a file behind for this package to
    have opinions about the permissions of.

    **The two are not equally private, though, and the flag is the weaker
    one.** A command line is public on a shared machine: ``/proc/<pid>/cmdline``
    is world-readable on Linux, so any other account can lift the token out of
    ``ps -eww`` for as long as the run lasts - on exactly the machine a token
    is worth having. An environment is not: ``/proc/<pid>/environ`` is 0400,
    readable by the owner alone. Shell history and an echoed CI command keep
    argv afterwards as well, and keep it after the run is over.

    The flag stays, because runs already use it and a credential that stops
    being accepted is a broken deployment rather than a fixed one. What it
    does not do any more is leak quietly: the help above says so, and
    :func:`resolve` advises once per run when a token actually arrives that
    way, naming the variable that does not have the problem.
    """
    group = parser.getgroup("failure-instrumentation")
    group.addoption(
        "--failure-instrumentation",
        action="store_true",
        default=False,
        dest=ENABLE_OPTION,
        help="Switch the plugin on for this run. Installed, it registers its "
        "hooks and its failure_* options and does nothing else until asked - "
        "here, by naming --callstack-port or --callstack-host, or by a call to "
        "pytest_failure_instrumentation.install(). Put it in addopts to have "
        "every run ask.",
    )
    group.addoption(
        "--failure-profile",
        action="store_true",
        default=False,
        dest=PROFILE_OPTION,
        help="Profile this run: sample every thread's stack and CPU on the "
        "process running the tests, and raise the functions that burnt the "
        "CPU and the tests that kept the memory as incidents. Switches the "
        "plugin on, like --callstack-port does. failure_profile in ini does "
        "the same for every run.",
    )
    group.addoption(
        "--failure-profile-allocations",
        action="store_true",
        default=False,
        dest=PROFILE_ALLOCATIONS_OPTION,
        help="Profile this run with allocation tracing as well: every memory "
        "finding names the lines holding the memory, and a memory flame graph "
        "is written beside the CPU one. Several times slower on "
        "allocation-heavy code, so for a rerun of the tests a plain --failure-profile "
        "named. Requires exclusive ownership of tracemalloc. Implies "
        "--failure-profile.",
    )
    group.addoption(
        "--callstack-port",
        type=int,
        default=None,
        metavar="PORT",
        help="Serve live stacks on this port, shared with any other session on "
        "the host that names the same one. Omit to draw a free port instead.",
    )
    group.addoption(
        "--callstack-host",
        default=None,
        metavar="HOST",
        help="Serve live stacks on this interface (default 127.0.0.1). Use "
        "0.0.0.0 to reach the server from outside a container.",
    )
    group.addoption(
        "--callstack-token",
        default=None,
        metavar="SECRET",
        help="Require this bearer token on every live-stack endpoint but "
        "/identity. Never written to disk. Required to bind anything but "
        "loopback; omit for no authentication, which is the default on "
        "loopback. Prefer PYTEST_CALLSTACK_TOKEN, which is read the same way: "
        "a token given here is in this process's command line, which any other "
        "user of the machine can read (ps -eww, /proc/<pid>/cmdline) for as "
        "long as the run lasts, and which shell history and CI logs keep "
        "afterwards. A process's environment is readable only by its owner.",
    )


#: The ``dest`` of ``--failure-instrumentation``: what ``getoption`` is asked for.
ENABLE_OPTION = "failure_instrumentation"
#: The ``dest`` of ``--failure-profile``.
PROFILE_OPTION = "failure_profile_this_run"
#: The ``dest`` of ``--failure-profile-allocations``.
PROFILE_ALLOCATIONS_OPTION = "failure_profile_allocations_this_run"
#: Faster than this and the sampler is most of what it measures.
MIN_PROFILE_INTERVAL = 0.002


def switched_on(config: pytest.Config) -> bool:
    """Whether this process was asked to run the plugin.

    Installed is not switched on. The entry point registers the hookspecs and
    the options on every run, so a conftest implementing
    ``pytest_failure_incident`` never fails ``check_pending`` and a ``failure_*``
    ini key is never an unknown option - and then does nothing more unless one
    of three things asked it to:

    ``--failure-instrumentation``, the switch itself.

    ``--callstack-port`` or ``--callstack-host``, because naming either is a
    request for the live-stack server (see :func:`_add_command_line_options`)
    and the server cannot run without the plugin under it. An option accepted,
    parsed and then ignored because a second switch was not also given is the
    behaviour that docstring exists to rule out. The token does not count, for
    the reason given there: it authenticates a server, it does not start one.

    Settings handed down by the controller, on an xdist worker. A controller
    that installed - by flag or by a framework calling :func:`install` - has
    asked for its workers too, and the framework's code may not even be
    loaded in the worker to ask again. The flag itself also reaches a worker,
    through the argv xdist replays there; this covers the install() case,
    where nothing is on the command line at all.

    :func:`install` is the fourth way and needs none of these: a caller that
    registers the plugin by hand has said what it wants by calling.
    """
    if _option(config, ENABLE_OPTION):
        return True
    if _option(config, "callstack_port") is not None:
        return True
    if _option(config, "callstack_host") is not None:
        return True
    if _option(config, PROFILE_OPTION) or _option(config, PROFILE_ALLOCATIONS_OPTION):
        # A request for a profile is a request for the plugin that takes it,
        # on the same grounds as the server options above.
        return True
    workerinput: dict[str, Any] = getattr(config, "workerinput", {}) or {}
    return bool(workerinput.get("failure_settings"))


def _option(config: pytest.Config, name: str) -> Any:
    """A command-line value, or None when there is not one to be had.

    Three ways there is not, and all of them are ordinary. The option may not
    be registered, because ``-p no:failure_instrumentation`` skips
    ``pytest_addoption`` - and asking pytest for an unregistered option raises
    rather than answering. The config may not be pytest's at all: a framework
    installing this by hand passes what it has. Or the option is registered and
    simply was not given, which is the common case and answers None by itself.
    """
    ask = getattr(config, "getoption", None)
    if ask is None:
        return None
    try:
        return ask(name)
    except (ValueError, KeyError):
        return None


def _ini(config: pytest.Config, name: str, fallback: Any) -> Any:
    """An ini value, or the fallback when the option is not registered at all.

    ``-p no:failure_instrumentation`` means ``add_options`` never ran, and a
    framework installing this by hand is exactly the case where that happens.
    Asking pytest for an unregistered ini key raises, so the absence is treated
    as "not configured" rather than allowed to end the run.
    """
    try:
        return config.getini(name)
    except (ValueError, KeyError):
        return fallback


def _flag(config: pytest.Config, name: str, fallback: bool) -> bool:
    raw = _ini(config, name, None)
    if raw is None:
        return fallback
    return str(raw).strip().lower() not in ("false", "0", "no", "")


def _number(config: pytest.Config, name: str, fallback: float) -> float:
    """A setting that cannot be read falls back, but says so.

    Silently substituting the default turns ``failure_stall_seconds = 5m`` into
    a stall detector the reader believes they configured and never hear from.
    """
    raw = _ini(config, name, None)
    try:
        return float(raw or fallback)
    except (ValueError, TypeError):
        advise(
            f"{name}={raw!r} is not a number; using {fallback} instead",
        )
        return fallback


def pytest_faulthandler_timeout(config: pytest.Config) -> float:
    """pytest's own ``faulthandler_timeout``, or 0 when nobody set one.

    There is exactly one ``faulthandler.dump_traceback_later`` timer per
    process, and arming it cancels whatever was armed before. pytest's
    faulthandler plugin arms it at the start of every test when this ini is
    set; the frozen-interpreter fallback re-arms it every second. Whichever
    ran last owns it, and the fallback always runs last - so a user who
    configured a timeout got a plugin that silently threw it away, including
    the ``faulthandler_exit_on_timeout`` that was supposed to end a hung run.

    Read here rather than in the worker so there is one answer to "is pytest
    using the timer", and it is read the same way pytest reads it.
    """
    try:
        return float(config.getini("faulthandler_timeout") or 0.0)
    except (ValueError, KeyError, TypeError):
        return 0.0


def _warn_if_the_token_was_typed_where_others_can_read_it() -> None:
    """``--callstack-token`` works and leaks; both halves are worth saying.

    A command line is not private. On Linux ``/proc/<pid>/cmdline`` is
    world-readable, and a controller lives for the length of the run, so any
    other account on the machine can take the token straight out of ``ps
    -eww`` - on the shared machine that is the reason to be running with a
    token at all. ``PYTEST_CALLSTACK_TOKEN`` reaches the same field by the
    same code path and does not have the problem: ``/proc/<pid>/environ`` is
    0400 and only the owner may read it. Shell history and an echoed CI
    command keep argv too, and keep it after the run has ended.

    Said as advice rather than a refusal, because the flag is documented, is
    in use, and is the right thing on a machine with one user on it. What is
    wrong is walking into the exposure without being told, so this fires when
    the token actually arrives that way - once per run, on the controller, and
    never on the runs that use the environment variable.

    The value is deliberately absent from the message. A warning about a
    credential being readable that quotes the credential into the warnings
    summary and the CI log has published it a second time.
    """
    advise(
        "--callstack-token puts the token in this run's command line, which is "
        "not private: any other user of this machine can read it out of ps -eww "
        "(or /proc/<pid>/cmdline) for as long as the run lasts, and shell "
        f"history and CI logs keep it afterwards. {TOKEN_ENV} is read the same "
        "way and does not leak - a process's environment is readable only by "
        "its owner. Nothing is refused: the token given is in force",
    )


def resolve(config: pytest.Config) -> Settings:
    """The settings this process should use, before anyone overrides them.

    On a worker the controller's copy wins where it exists: a framework that
    built its settings in Python has no ini for the worker to re-read, and even
    where there is one, two processes resolving separately is two chances to
    disagree.
    """
    workerinput: dict[str, Any] = getattr(config, "workerinput", {}) or {}
    worker_count = int(workerinput.get("workercount", 1) or 1)
    # Pushed into workerinput by the controller (see IncidentEngine's
    # pytest_configure_node), so both sides of a run stamp their evidence with
    # the same id and a stale file from an earlier run is recognisable.
    run_id = str(workerinput.get("failure_run_id") or "") or None

    # The command line outranks ini for these two: they answer "where does this
    # run happen", which the person starting it knows and the repository does
    # not.
    chosen_port = _option(config, "callstack_port")
    chosen_host = _option(config, "callstack_host")
    named_on_cli = chosen_port is not None or chosen_host is not None
    # Command line first, environment second, and no third place to look. A
    # token does not switch the server on: "authenticate the server I am
    # already running" and "start a server" are different requests, and an
    # exported PYTEST_CALLSTACK_TOKEN sitting in a shell profile must not turn
    # a listening socket on for every pytest run in that shell.
    chosen_token = _option(config, "callstack_token")
    # Which of the two the token came from has to be settled here, because it
    # is the last place that knows: a Settings carries the value and not its
    # provenance, and the two forms are not equally private - see
    # _warn_if_the_token_was_typed_where_others_can_read_it.
    from_the_command_line = bool(str(chosen_token or "").strip())
    if chosen_token is None:
        chosen_token = os.environ.get(TOKEN_ENV)

    handed_down = workerinput.get("failure_settings")
    if handed_down:
        return Settings.from_payload(
            dict(handed_down), worker_count=worker_count, run_id=run_id
        )

    # After the worker's early return, so this is said once by the controller
    # rather than once per worker. A worker is handed the settings and never
    # the token anyway, but it does parse the same argv.
    if from_the_command_line:
        _warn_if_the_token_was_typed_where_others_can_read_it()

    return Settings(
        directory=Path(_ini(config, "failure_directory", "") or DEFAULT_DIRECTORY),
        packages=tuple(_ini(config, "failure_packages", ()) or ()),
        product_version=_ini(config, "failure_product_version", "") or None,
        watchdog=_flag(config, "failure_watchdog", True),
        heartbeat_interval=_number(config, "failure_heartbeat_interval", 5.0),
        tracemalloc_depth=int(_number(config, "failure_tracemalloc_depth", 0)),
        object_census=_flag(config, "failure_object_census", False),
        high_water_mb=int(_number(config, "failure_high_water_mb", 0)),
        memory_limit_mb=int(_number(config, "failure_memory_limit_mb", 0)),
        slow_test_seconds=_number(config, "failure_slow_test_seconds", 20.0),
        stall_seconds=_number(config, "failure_stall_seconds", 300.0),
        stack_probe=_flag(config, "failure_stack_probe", True),
        crash_stack=_flag(config, "failure_crash_stack", False),
        kill_trace=_flag(config, "failure_kill_trace", True),
        capture_output=_flag(config, "failure_capture_output", False),
        elevate=_flag(config, "failure_elevate", False),
        on_run_death=str(_ini(config, "failure_on_run_death", "") or "").strip() or None,
        tracer=str(_ini(config, "failure_tracer", "parent") or "parent").strip().lower(),
        sample_seconds=_number(config, "failure_sample_seconds", 0.0),
        stack_server=_flag(config, "failure_stack_server", False) or named_on_cli,
        stack_server_port=(
            chosen_port
            if chosen_port is not None
            else int(_number(config, "failure_stack_server_port", PORT_LOTTERY))
        ),
        stack_server_host=(
            chosen_host
            or _ini(config, "failure_stack_server_host", "")
            or DEFAULT_STACK_SERVER_HOST
        ),
        stack_server_locals=_flag(config, "failure_stack_server_locals", True),
        stack_server_token=chosen_token or "",
        worker_count=worker_count,
        run_id=run_id,
        # Asking for allocation tracing is asking for the profile it traces
        # under, from the ini as from the command line.
        profile=_flag(config, "failure_profile", False)
        or _flag(config, "failure_profile_allocations", False)
        or bool(_option(config, PROFILE_OPTION))
        or bool(_option(config, PROFILE_ALLOCATIONS_OPTION)),
        profile_interval=_number(config, "failure_profile_interval", 0.02),
        profile_cpu_share=_number(config, "failure_profile_cpu_share", 5.0),
        profile_cpu_floor_seconds=_number(config, "failure_profile_cpu_floor_seconds", 0.5),
        profile_retained_mb=int(_number(config, "failure_profile_retained_mb", 100)),
        profile_peak_mb=int(_number(config, "failure_profile_peak_mb", 0)),
        profile_allocations=_flag(config, "failure_profile_allocations", False)
        or bool(_option(config, PROFILE_ALLOCATIONS_OPTION)),
        profile_allocation_depth=int(_number(config, "failure_profile_allocation_depth", 12)),
        profile_burst_cores=_number(config, "failure_profile_burst_cores", 0.7),
        profile_burst_seconds=_number(config, "failure_profile_burst_seconds", 2.0),
    )
