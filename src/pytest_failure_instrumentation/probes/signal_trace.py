"""Who sent the signal: the kernel's ``signal_generate`` tracepoint, watched
from a sidecar with the privilege it needs.

The wait status says a process died of SIGKILL and nothing else. The one
place the sender is recorded is the kernel, at the moment it queues the signal:
the ``signal:signal_generate`` tracepoint fires in the *sender's* context, so
its line carries the sender's comm and pid in front and the target's after,
together with ``si_code`` - ``0`` (``SI_USER``) for a ``kill(2)`` from a
process, ``128`` (``SI_KERNEL``) for the kernel's own kills, the OOM killer
included. That is the difference between "SIGKILL, could be anything" and
"SIGKILL from ``containerd-shim`` pid 812" or "SIGKILL from the kernel, in the
context of gw7 allocating"::

    python-1771  [000] d..1.  401.375501: signal_generate: sig=9 errno=0 code=0 comm=sleep pid=1772 grp=1 res=0

Reading tracepoints needs root: tracefs is ``0700`` and ``perf_event_paranoid``
gates the alternative. So it is done by a *sidecar* - a second interpreter
running only the stdlib script below, started directly where this process is
already root and through ``sudo -n`` where it is not and
``failure_elevate`` allows it. ``-n`` means a sudo that would prompt fails
instead of hanging an unattended run.

It never touches the machine's tracer. Tracefs has *instances* - a directory
made under ``instances/`` is a whole separate tracer with its own buffer and
its own event switches - so the sidecar makes one named for this run, enables
one event in it with a filter on the two signals that matter, and removes it
on the way out. Somebody's ``perf`` or ``trace-cmd`` on the same machine is
untouched, and a sidecar that dies uncleanly leaves an empty instance
directory rather than a global tracer left running.

What the sidecar writes is one JSON line per event to a file in the run's
directory, stamped with the wall clock as it read it: it is reading the pipe
live, so that stamp is the event's time to within a millisecond and needs no
conversion from the kernel's clock. The sender's command line and executable
are read out of ``/proc`` in the same instant, because the sender of a
``kill -9`` is very often a process that exists for that one syscall and is
gone by the time anybody else looks.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .platform_flags import IS_LINUX, IS_WINDOWS

TRACEFS_ROOTS = ("/sys/kernel/tracing", "/sys/kernel/debug/tracing")
EVENT = "events/signal/signal_generate"
INSTANCE_PREFIX = "pytest-failure-"
#: The file the sidecar writes in the run's directory.
TRACE_FILE = "signals.log"
#: What is traced. SIGKILL is the one that cannot be witnessed any other way;
#: SIGTERM is the warning shot every orchestrator sends first, and knowing who
#: sent it explains the SIGKILL that follows.
TRACED_SIGNALS = (9, 15)

SI_USER = 0
SI_KERNEL = 128
SI_QUEUE = -1
SI_TKILL = -6

#: One line of ``trace_pipe``. The sender's comm may hold a space; it ends at
#: the last ``-<pid>`` before the CPU column.
TRACE_LINE = re.compile(
    r"^\s*(?P<sender_comm>.*?)-(?P<sender_pid>\d+)\s+\[\d+\]\s+\S+\s+(?P<ts>\d+\.\d+):\s+"
    r"signal_generate:\s+sig=(?P<sig>\d+)\s+errno=-?\d+\s+code=(?P<code>-?\d+)\s+"
    r"comm=(?P<comm>.*?)\s+pid=(?P<pid>\d+)\s+grp=(?P<grp>\d+)\s+res=(?P<res>\d+)"
)


@dataclass
class Witness:
    """One signal the kernel generated, and who asked it to."""

    at: Optional[float]
    trace_seconds: float
    signal: int
    si_code: int
    sender_pid: int
    sender_comm: str
    sender_cmdline: Optional[str]
    sender_exe: Optional[str]
    target_pid: int
    target_comm: str
    to_group: bool
    delivered: bool
    #: ``signal`` for a Linux tracepoint line; ``TerminateProcess`` for a
    #: Windows termination, where ``signal`` is 0 and ``exit_code`` is what
    #: the caller chose - see :mod:`.etw_trace`.
    via: str = "signal"
    exit_code: Optional[int] = None

    @property
    def from_kernel(self) -> bool:
        return self.via == "signal" and self.si_code == SI_KERNEL


# -- can this machine do it -------------------------------------------------


def tracefs_root() -> Optional[str]:
    for root in TRACEFS_ROOTS:
        if os.path.isdir(os.path.join(root, "events")):
            return root
    return None


_sudo_verdict: Optional[bool] = None


def sudo_works() -> bool:
    """Whether ``sudo -n`` grants root here without asking anything.

    Asked once per process: the answer does not change mid-run, and the
    question costs a fork.
    """
    global _sudo_verdict
    if _sudo_verdict is None:
        try:
            done = subprocess.run(
                ["sudo", "-n", "true"], capture_output=True, timeout=10
            )
            _sudo_verdict = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _sudo_verdict = False
    return _sudo_verdict


def availability(elevate: bool) -> tuple[bool, str]:
    """Whether the tracepoint can be watched, and how - or why not.

    The second value is recorded on incidents either way, so a reader of
    ``SIGKILLED`` sees which source was withheld and by what, rather than an
    absence.
    """
    if IS_WINDOWS:
        # Windows keeps the same record through ETW, and an ETW session needs
        # administrator rights or Performance Log Users; there is no sudo to
        # elevate with, so the run either has it or it does not. Whether it
        # does is learned by starting, and said then.
        return True, "etw"
    if not IS_LINUX:
        return False, "tracepoints are a Linux source"
    root = tracefs_root()
    if root is None:
        return False, "tracefs is not mounted (mount -t tracefs tracefs /sys/kernel/tracing)"
    if not os.path.exists(os.path.join(root, EVENT)):
        return False, "this kernel has no signal_generate tracepoint"
    if os.geteuid() == 0 and os.access(os.path.join(root, "instances"), os.W_OK):
        return True, "tracefs"
    if elevate:
        if sudo_works():
            return True, "sudo tracefs"
        return False, "tracefs needs root and sudo -n was refused"
    return False, "tracefs needs root; set failure_elevate to use sudo"


# -- the sidecar ------------------------------------------------------------

#: Stdlib only, and it must stay that way: it runs under sudo with a reset
#: environment, in whatever interpreter ``sys.executable`` is, and must not
#: need this package importable there. argv: instance name, filter, output
#: path, ``uid:gid`` to hand the output file to, and the mode - ``trace``,
#: or ``watch`` for a sidecar that traces nothing and exists to report the
#: run's death (see :mod:`..incidents.reporter`). Its stdin carries the run's
#: messages: the reporter payload after start, ``stop`` at session finish.
SIDECAR = r'''
import json, os, select, signal, subprocess, sys, time

instance, event_filter, output, owner, mode = sys.argv[1:6]
ROOTS = ("/sys/kernel/tracing", "/sys/kernel/debug/tracing")
EVENT = "events/signal/signal_generate"
BUFFER_KB = 64  # per CPU; an event is under 200 bytes and the pipe is read live
REPORTER = "from pytest_failure_instrumentation.incidents.reporter import main; main()"
REPORTER_TIMEOUT = 300.0
tracing = mode == "trace"


class Stop(Exception):
    pass


def stop(*_):
    raise Stop


def write(path, text):
    with open(path, "w") as handle:
        handle.write(text)


def proc(pid, name):
    try:
        with open("/proc/%d/%s" % (pid, name), "rb") as handle:
            return handle.read()
    except OSError:
        return None


try:
    uid, gid = (int(part) for part in owner.split(":"))
except ValueError:
    uid = gid = None


def own(path):
    if uid is None:
        return
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def report(payload):
    """The run that started this died without saying goodbye.

    Hand what it left to the reporter - a child of this process, running as
    the user that started the run, with that user's environment and groups.
    Nothing of the user's runs here, where this may be root.
    """
    directory = os.path.dirname(os.path.abspath(output))
    log_path = os.path.join(directory, "reporter.log")
    log = open(log_path, "ab")
    own(log_path)
    extra = {}
    if hasattr(os, "geteuid") and os.geteuid() == 0 and uid:
        extra = {"user": uid, "group": gid}
        try:
            import pwd
            extra["extra_groups"] = os.getgrouplist(pwd.getpwuid(uid).pw_name, gid)
        except (KeyError, OSError, ImportError):
            pass
    try:
        child = subprocess.Popen(
            [payload.get("python") or sys.executable, "-c", REPORTER],
            stdin=subprocess.PIPE, stdout=log, stderr=log,
            cwd=payload.get("rootdir") or None, env=payload.get("env") or None, **extra
        )
        child.stdin.write(json.dumps(payload).encode("utf-8"))
        child.stdin.close()
        child.wait(timeout=REPORTER_TIMEOUT)
    except Exception as failure:
        log.write(("the reporter could not be run: %r\n" % (failure,)).encode("utf-8"))
    finally:
        log.close()


here = None
event = None
if tracing:
    root = next((r for r in ROOTS if os.path.isdir(os.path.join(r, "events"))), None)
    if root is None:
        sys.exit(3)
    instances = os.path.join(root, "instances")
    # Sweep what a sidecar that was itself killed left behind: an instance
    # named for a pid that is no longer running, still tracing for nobody.
    # Ours are the only ones touched, and only those whose owner is gone.
    prefix = instance.rsplit("-", 1)[0] + "-"
    for name in os.listdir(instances):
        owner_pid = name[len(prefix):] if name.startswith(prefix) else ""
        if not owner_pid.isdigit() or os.path.exists("/proc/" + owner_pid):
            continue
        stale = os.path.join(instances, name)
        try:
            write(os.path.join(stale, EVENT, "enable"), "0")
        except OSError:
            pass
        try:
            os.rmdir(stale)
        except OSError:
            pass
    here = os.path.join(instances, instance)
    try:
        os.mkdir(here)
    except FileExistsError:
        pass  # a previous sidecar of this run died without removing it
    # A fresh instance gets the kernel's default ring buffer, about 1.4 MB
    # per CPU - some 90 MB on a 64-core runner, for a stream of a few events
    # that is read as it arrives. Shrunk before the event is enabled; a
    # kernel that refuses keeps its default, which costs memory and nothing
    # else.
    try:
        write(os.path.join(here, "buffer_size_kb"), str(BUFFER_KB))
    except OSError:
        pass
    event = os.path.join(here, EVENT)
    write(os.path.join(event, "filter"), event_filter)
    write(os.path.join(event, "enable"), "1")

out = open(output, "a", buffering=1)
own(output)
try:
    boot_id = open("/proc/sys/kernel/random/boot_id").read().strip()
except OSError:
    boot_id = None
out.write(json.dumps({
    "header": True, "pid": os.getpid(), "mode": mode, "instance": here, "boot_id": boot_id,
    "wall": time.time(), "monotonic": time.clock_gettime(time.CLOCK_MONOTONIC),
    "filter": event_filter if tracing else None,
}) + "\n")

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
pipe = os.open(os.path.join(here, "trace_pipe"), os.O_RDONLY | os.O_NONBLOCK) if tracing else None
watched = [0] + ([pipe] if tracing else [])
pending = b""
inbound = b""
payload = None
told_to_stop = False
orphaned = False
# Once stdin reaches EOF - the run that started this is gone, or asked it to
# stop - the pipe is drained for a moment longer rather than dropped: the last
# event in it may be the SIGKILL that ended the run, which is the one line a
# later run needs to say who did it.
closing_at = None
try:
    while True:
        if closing_at is not None and time.monotonic() >= closing_at:
            break
        ready, _, _ = select.select(watched, [], [], 0.1 if closing_at else 1.0)
        if 0 in ready and closing_at is None:
            chunk = os.read(0, 65536)
            if not chunk:
                orphaned = not told_to_stop
                closing_at = time.monotonic() + (0.5 if tracing else 0.0)
                watched = [pipe] if tracing else []
                if not tracing:
                    break
            else:
                # The run's messages: the reporter payload at its start, and
                # "stop" at its end. EOF without "stop" is a death.
                inbound += chunk
                lines = inbound.split(b"\n")
                inbound = lines.pop()
                for raw in lines:
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    if message.get("stop"):
                        told_to_stop = True
                    if isinstance(message.get("reporter"), dict):
                        payload = message["reporter"]
        if not tracing:
            continue
        try:
            chunk = os.read(pipe, 65536)
        except BlockingIOError:
            continue
        if not chunk:
            continue
        pending += chunk
        lines = pending.split(b"\n")
        pending = lines.pop()
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            record = {"line": line, "wall": round(time.time(), 6)}
            head = line.split("[", 1)[0].rstrip()
            sender = head.rsplit("-", 1)[-1]
            if sender.isdigit():
                cmdline = proc(int(sender), "cmdline")
                if cmdline is not None:
                    record["sender_cmdline"] = cmdline.decode("utf-8", "replace").replace("\0", " ").strip()
                try:
                    record["sender_exe"] = os.readlink("/proc/%s/exe" % sender)
                except OSError:
                    pass
            out.write(json.dumps(record) + "\n")
except Stop:
    pass
finally:
    if tracing:
        try:
            write(os.path.join(event, "enable"), "0")
        except OSError:
            pass
        try:
            os.close(pipe)
        except OSError:
            pass
        try:
            os.rmdir(here)
        except OSError:
            pass
    out.close()
if orphaned and payload is not None:
    report(payload)
'''


class SignalTracer:
    """One sidecar for one run, writing to one file."""

    def __init__(
        self,
        output: Path,
        elevate: bool = False,
        signals: tuple[int, ...] = TRACED_SIGNALS,
        reporter: Optional[dict[str, Any]] = None,
        trace: bool = True,
    ) -> None:
        self.output = output
        self.elevate = elevate
        self.signals = signals
        #: Whether to trace at all. Off, the sidecar exists only for the
        #: reporter, in watch mode - ``failure_kill_trace`` is one promise
        #: and ``failure_on_run_death`` another.
        self.trace = trace
        #: What the sidecar hands the reporter if this run dies - see
        #: :mod:`..incidents.reporter`. With one, a sidecar is started even
        #: where nothing can be traced, in a watch-only mode, because the
        #: reporter needs no privilege and a killed run needs a survivor.
        self.reporter = reporter
        self.process: Optional[subprocess.Popen[bytes]] = None
        #: The tracing status: how, or why not. Independent of whether a
        #: watch-only sidecar is running for the reporter.
        self.how = "off"

    def start(self) -> str:
        """Start watching; returns how, or a reason it could not.

        Never raises: this runs at session start, and a witness that cannot
        be started is a line on the incident, not the end of the run.
        """
        try:
            return self._start()
        except Exception as failure:  # noqa: BLE001 - see the docstring
            self.how = f"off: failed ({failure!r})"
            self.stop()
            return self.how

    def _start(self) -> str:
        if not self.trace:
            self.how = "off: failure_kill_trace is off"
            if self.reporter is not None and self._spawn("watch", "watch"):
                self._send({"reporter": self.reporter})
            return self.how
        usable, how = availability(self.elevate)
        if usable:
            self.how = how if self._spawn(how, "trace") else self.how
            if self.active:
                self._send({"reporter": self.reporter})
                return self.how
        else:
            self.how = f"off: {how}"
        # Nothing traces, or tracing could not be started. With a reporter
        # configured a sidecar is still owed: one that watches for the run's
        # death and nothing else, which needs no privilege at all.
        if self.reporter is not None and self._spawn(how if usable else "watch", "watch"):
            self._send({"reporter": self.reporter})
        return self.how

    def _spawn(self, how: str, mode: str) -> bool:
        """Start one sidecar; True if it came up. ``self.how`` says why not."""
        creation: dict[str, Any] = {}
        if IS_WINDOWS:
            session = f"{INSTANCE_PREFIX}{os.getpid()}"
            command = [
                sys.executable, "-c",
                "from pytest_failure_instrumentation.probes.etw_trace import main; main()",
                session, str(self.output), mode,
            ]
            # No console window of its own, and no share in this one's
            # Ctrl-C: the sidecar ends when its stdin does.
            creation["creationflags"] = 0x08000000 | 0x00000200
        else:
            instance = f"{INSTANCE_PREFIX}{os.getpid()}"
            event_filter = "||".join(f"sig=={number}" for number in self.signals)
            owner = f"{os.getuid()}:{os.getgid()}"
            # sudo only for tracing: a watch-only sidecar has no use for root
            # and must not have it.
            elevated = ["sudo", "-n"] if (how == "sudo tracefs" and mode == "trace") else []
            command = elevated + [
                sys.executable, "-c", SIDECAR, instance, event_filter, str(self.output), owner, mode,
            ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **creation,
            )
        except OSError as failure:
            self.how = f"off: the sidecar could not be started ({failure!r})"
            return False
        if not self._came_up():
            code = self.process.poll()
            self.how = (
                _exit_meaning(code, how)
                if code is not None
                else "off: the sidecar did not start in time"
            )
            self.stop()
            return False
        return True

    def _send(self, message: dict[str, Any]) -> None:
        """One line to the sidecar. Lost quietly if it is gone: the run
        goes on either way."""
        process = self.process
        if process is None or process.stdin is None:
            return
        try:
            process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            process.stdin.flush()
        except (OSError, ValueError, TypeError):
            pass

    def _came_up(self, timeout: float = 5.0) -> bool:
        """The header line is the sidecar saying the event is enabled."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                return False
            if header(self.output) is not None:
                return True
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        try:
            self._stop()
        except Exception:  # noqa: BLE001 - session finish must not fail here
            pass

    def _stop(self) -> None:
        process = self.process
        if process is None:
            return
        # "stop" first, then EOF: EOF alone is what a dead run looks like,
        # and would have the sidecar report this run as killed.
        self._send({"stop": True})
        self.process = None
        # Closing its stdin is the request; the sidecar polls for it. SIGTERM
        # is the fallback, and would not reach a sudo child from a non-root
        # parent on every configuration, which is why the request comes first.
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=3.0)
            return
        except subprocess.TimeoutExpired:
            pass
        for stopper in (process.terminate, process.kill):
            try:
                stopper()
                process.wait(timeout=2.0)
                return
            except (OSError, subprocess.TimeoutExpired):
                continue

    @property
    def active(self) -> bool:
        return self.process is not None and self.process.poll() is None


# -- reading what it wrote --------------------------------------------------


def _exit_meaning(code: int, how: str) -> str:
    if how == "etw" and code == 5:
        return (
            "off: the ETW session was refused (needs administrator rights, or "
            "membership of Performance Log Users)"
        )
    return f"off: the sidecar exited with status {code} before tracing"


#: How much of a trace file is read when a death is being explained. One line
#: per SIGKILL or SIGTERM on the whole machine, so a busy host running many
#: short-lived containers can write a great deal of it; the kill that ended a
#: process of this run is at the end.
TAIL_BYTES = 16 * 1024 * 1024


def header(path: Path) -> Optional[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            first = handle.readline()
    except OSError:
        return None
    try:
        record = json.loads(first)
    except ValueError:
        return None
    return record if isinstance(record, dict) and record.get("header") else None


def witnessed(path: Path) -> list[Witness]:
    """Every signal the sidecar saw, in order - Linux lines and Windows ones."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]  # the seek landed inside it
    found: list[Witness] = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("header"):
            continue
        witness = parse_record(record)
        if witness is not None:
            found.append(witness)
    return found


def parse_record(record: dict[str, Any]) -> Optional[Witness]:
    """One of the sidecar's JSON lines, whichever sidecar wrote it."""
    if record.get("via") == "TerminateProcess":
        try:
            sender, target = int(record["sender_pid"]), int(record["target_pid"])
        except (KeyError, TypeError, ValueError):
            return None
        at = record.get("wall") if isinstance(record.get("wall"), (int, float)) else None
        return Witness(
            at=float(at) if at is not None else None,
            trace_seconds=0.0,
            signal=0,
            si_code=SI_USER,
            sender_pid=sender,
            sender_comm=str(record.get("sender_comm") or ""),
            sender_cmdline=None,
            sender_exe=record.get("sender_exe"),
            target_pid=target,
            target_comm="",
            to_group=False,
            delivered=True,
            via="TerminateProcess",
            exit_code=record.get("exit_code") if isinstance(record.get("exit_code"), int) else None,
        )
    witness = parse_line(str(record.get("line", "")), record.get("wall"))
    if witness is None:
        return None
    witness.sender_cmdline = record.get("sender_cmdline")
    witness.sender_exe = record.get("sender_exe")
    return witness


def parse_line(line: str, at: Optional[float] = None) -> Optional[Witness]:
    match = TRACE_LINE.match(line)
    if match is None:
        return None
    return Witness(
        at=at,
        trace_seconds=float(match.group("ts")),
        signal=int(match.group("sig")),
        si_code=int(match.group("code")),
        sender_pid=int(match.group("sender_pid")),
        sender_comm=match.group("sender_comm").strip(),
        sender_cmdline=None,
        sender_exe=None,
        target_pid=int(match.group("pid")),
        target_comm=match.group("comm"),
        to_group=match.group("grp") == "1",
        delivered=match.group("res") == "0",
    )


def sent_to(path: Path, pid: int, signal: Optional[int] = None) -> list[Witness]:
    """What was sent to one process, oldest first."""
    return [
        witness
        for witness in witnessed(path)
        if witness.target_pid == pid and (signal is None or witness.signal == signal)
    ]
