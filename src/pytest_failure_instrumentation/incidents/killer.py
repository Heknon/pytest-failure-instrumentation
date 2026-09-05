"""Who killed it, from the things that keep a record of killing.

A wait status of ``-9`` is the one number designed to say nothing about who
sent the signal. Everything that actually ends a process keeps a record
somewhere else, and this module goes and asks each of them:

* **The kernel's signal tracepoint** (:mod:`..probes.signal_trace`), which
  names the sender of every SIGKILL and SIGTERM to any process on the machine
  - a userspace pid and comm, or the kernel itself. Needs root, or ``sudo``
  when ``failure_elevate`` allows it.
* **The kernel log** (:mod:`..probes.kernel_log`), where the OOM killer prints
  the victim, the constraint it hit, the cgroup, and the table of every task
  it weighed - the whole fleet at the instant of the decision.
* **Historical controller SIGTERM records** (:mod:`..capture.signals`), read
  for compatibility with older evidence. Current runs preserve the caller's
  signal masks and handlers and do not install that witness.

Each answers a different question, and each can be withheld by a machine - a
container without tracefs, ``dmesg_restrict=1`` without ``CAP_SYSLOG``, a
platform without a supported trace source. So the result carries not only what was
found but what each source *said about itself*, and a verdict that stays
``SIGKILLED`` names which truth was withheld and by what. That is the
difference between a guess and a finding that happens to be negative.
"""

from __future__ import annotations

import json
import os
import signal as signal_module
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..capture import events as event_log
from ..capture import signals as controller_signals
from ..capture.state import read_state
from ..probes import kernel_log, signal_trace

#: The controller event log, including legacy signal-witness evidence when
#: recovering a run recorded by an older version.
CONTROLLER_EVENTS = event_log.CONTROLLER_EVENTS
#: The role the controller's pid is filed under, so a signal it sent - execnet
#: terminating a worker that would not exit - reads as what it is.
CONTROLLER = "the controller"

#: How far before a death a SIGTERM is still taken as its explanation. Docker
#: waits ten seconds between the two; kubelet thirty; systemd ninety; GitLab's
#: shell executor a full ten minutes. Beyond this the two are separate events.
TERM_WINDOW_SECONDS = 660.0
#: Slack after the controller noticed a death, for a record that landed a
#: moment later than the notice.
AFTER_DEATH_SECONDS = 5.0
#: How long a *live* death waits for its line to reach the trace file. The
#: controller learns of a death within milliseconds of it; the Linux sidecar
#: writes within a few more, and ETW flushes its real-time buffers on a
#: one-second timer. Waited only while nothing for this pid is there yet,
#: and never for a death found afterwards - its file is as complete as it
#: will ever be.
TRACE_SETTLE_SECONDS = 2.0 if sys.platform == "win32" else 0.5
TRACE_POLL_SECONDS = 0.05


class SignalRecord(BaseModel):
    """One signal somebody sent to a process of this run, and who."""

    model_config = ConfigDict(extra="forbid")

    #: The signal number, or 0 for a Windows ``TerminateProcess``, whose
    #: ``name`` says so. ``exit_code`` is the observed victim exit status;
    #: ``api_status`` is the termination API result reported by ETW.
    signal: int
    name: str = ""
    exit_code: Optional[int] = None
    api_status: Optional[int] = None
    at: Optional[float] = None
    #: Measured from when the controller learned of the death, so it is a
    #: lower bound on how long before the process actually ended.
    seconds_before_death: Optional[float] = None
    si_code: Optional[int] = None
    #: ``process``: a userspace ``kill(2)``. ``kernel``: the kernel's own, the
    #: OOM killer above all. ``self``: the process signalled itself.
    origin: str = "unknown"
    sender_pid: Optional[int] = None
    sender_uid: Optional[int] = None
    sender_comm: Optional[str] = None
    sender_cmdline: Optional[str] = None
    sender_exe: Optional[str] = None
    #: Where the sender stands relative to this run: ``itself``, ``this run's
    #: controller``, ``gw3, another process of this run``, or ``outside this
    #: run`` - which is the case that means somebody stopped it on purpose.
    sender_role: Optional[str] = None
    #: Who it was sent to: ``this worker`` or ``the controller``.
    target: str = ""
    #: ``signal-trace`` (the kernel tracepoint) or ``controller-witness``
    #: (the controller's own siginfo).
    source: str = ""

    def who(self) -> str:
        """The sender process, as a reader would want it said.

        For a kernel-originated signal this is the process in whose context
        the kernel acted - the allocator that hit the limit - and the caller
        says so around it; the kernel is not a "who".
        """
        if self.origin == "self":
            return "itself"
        # The comm is fifteen characters of whatever the kernel had, and for
        # an interpreter fed a script on stdin that is ``<...>`` or ``-``;
        # the executable's name says more, and the command line most of all.
        name = self.sender_comm or ""
        if (not name or not any(char.isalnum() for char in name)) and self.sender_exe:
            name = self.sender_exe.rsplit("/", 1)[-1]
        name = name or "an unnamed process"
        parts = [name]
        if self.sender_pid is not None:
            parts.append(f"pid {self.sender_pid}")
        if self.sender_uid is not None:
            parts.append(f"uid {self.sender_uid}")
        described = f"{parts[0]} ({', '.join(parts[1:])})" if len(parts) > 1 else parts[0]
        if self.sender_role and self.sender_role != "itself":
            described += f", {self.sender_role}"
        if self.sender_cmdline and self.sender_cmdline != self.sender_comm:
            described += f" - `{self.sender_cmdline[:120]}`"
        return described


class OomKillRecord(BaseModel):
    """What the kernel printed when it chose this process, and the fleet
    around it at that instant."""

    model_config = ConfigDict(extra="forbid")

    victim_pid: int
    victim_comm: str = ""
    at: Optional[float] = None
    #: ``pid``: the log names this worker's pid. ``cgroup and time``: the pid
    #: in the log is another namespace's, and the kill landed in this run's
    #: cgroup at the moment this worker died.
    matched_by: str = "pid"
    constraint: Optional[str] = None
    memcg: Optional[str] = None
    task_memcg: Optional[str] = None
    anon_rss_mb: Optional[int] = None
    file_rss_mb: Optional[int] = None
    shmem_rss_mb: Optional[int] = None
    total_vm_mb: Optional[int] = None
    uid: Optional[int] = None
    oom_score_adj: Optional[int] = None
    #: Which rung of the kernel-log ladder answered.
    source: str = ""
    #: The process in whose context the kernel made the kill - the one whose
    #: allocation hit the limit - when the tracepoint saw it. Often not the
    #: victim: gw7 allocates, the kernel chooses gw3.
    triggered_by_pid: Optional[int] = None
    triggered_by_comm: Optional[str] = None
    triggered_by_role: Optional[str] = None
    #: The table: how many tasks the killer weighed and their total RSS; how
    #: many were this run's and their total; where the victim ranked by size.
    tasks_considered: Optional[int] = None
    tasks_rss_mb: Optional[int] = None
    run_tasks: Optional[int] = None
    run_rss_mb: Optional[int] = None
    run_median_rss_mb: Optional[int] = None
    victim_rank: Optional[int] = None
    largest: list[dict[str, Any]] = Field(default_factory=list)
    #: ``fleet``: the victim was an ordinary member of a run that together
    #: exceeded the limit. ``own weight``: it was far above its peers.
    #: ``single``: it was the only process of this run in the table.
    pressure: Optional[str] = None


class KillSources(BaseModel):
    """What each source said about itself, so an absence is never silent."""

    model_config = ConfigDict(extra="forbid")

    kernel_log: str = "not consulted"
    signal_trace: str = "off"
    controller_witness: str = "off"


@dataclass
class Sources:
    """Where this run keeps what its witnesses wrote, and what they were
    allowed to do."""

    directory: Path
    elevate: bool = False
    trace_status: str = "off"
    witness_status: str = "off"
    run_pids: Optional[Callable[[], dict[int, str]]] = None
    #: Whether the sidecar is still writing: a death noticed live may have
    #: to wait a moment for its line, one found afterwards never does.
    live: bool = True
    #: The last kernel-log reading this run made: when it was taken, the
    #: window it opened at, and what it found. One object serves every death
    #: of a run - see :meth:`kernel_log_reading`.
    _log: Optional[tuple[float, Optional[float], kernel_log.KernelLogReading]] = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def trace_path(self) -> Path:
        return self.directory / signal_trace.TRACE_FILE

    @property
    def witness_path(self) -> Path:
        return self.directory / CONTROLLER_EVENTS

    _trace_cache: Optional[tuple[tuple[int, int, int], list[signal_trace.Witness]]] = field(
        default=None, init=False, repr=False)
    _trace_waited_at: float = field(default=-100.0, init=False, repr=False)
    _refresh: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _ready: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _last_attempt: float = field(default=-100.0, init=False, repr=False)

    def kernel_log_reading(
        self, since: Optional[float], died_at: float
    ) -> kernel_log.KernelLogReading:
        """Share a snapshot across a cascade; live log I/O is off the controller.

        One short wait per refresh, never a wait per worker. A snapshot cannot
        prove that a later kill did not happen, so its age is explicit and
        absence never produces an OOM verdict. Recovery has no live scheduler
        and can read synchronously.
        """
        if not self.live:
            cached = self._log
            if cached is None or cached[0] < died_at or (
                cached[1] is not None and (since is None or since < cached[1])
            ):
                self._log = (time.time(), since, kernel_log.read(since=since, elevate=self.elevate))
            cached = self._log
            assert cached is not None
            return kernel_log.narrowed(cached[2], since)
        started = False
        with self._lock:
            if (self._refresh is None or not self._refresh.is_alive()) and (
                time.monotonic() - self._last_attempt >= 2.0
            ):
                self._last_attempt = time.monotonic()
                self._ready.clear()
                def refresh() -> None:
                    try:
                        reading = kernel_log.read(since=None, elevate=self.elevate)
                        self._log = (time.time(), None, reading)
                    except Exception as failure:  # noqa: BLE001 - diagnostic I/O must not escape
                        self._log = (time.time(), None, kernel_log.KernelLogReading(
                            [], "unavailable", f"kernel log collection failed ({failure!r})"))
                    finally:
                        self._ready.set()
                self._refresh = threading.Thread(target=refresh, daemon=True,
                                                 name="failure-kernel-log")
                self._refresh.start()
                started = True
        if started:
            self._ready.wait(0.1)
        cached = self._log
        if cached is None:
            return kernel_log.KernelLogReading([], "unavailable", "kernel log collection pending")
        reading = kernel_log.narrowed(cached[2], since)
        age = max(0.0, died_at - cached[0])
        return kernel_log.KernelLogReading(reading.kills, reading.source,
                                          f"{reading.detail}; snapshot age {age:.1f}s")

    def trace_reading(self) -> list[signal_trace.Witness]:
        try:
            info = self.trace_path.stat()
            key = (info.st_ino, info.st_size, info.st_mtime_ns)
        except OSError:
            return []
        if self._trace_cache is None or self._trace_cache[0] != key:
            self._trace_cache = (key, signal_trace.witnessed(self.trace_path))
        return self._trace_cache[1]


@dataclass
class Attribution:
    killer: Optional[SignalRecord]
    oom: Optional[OomKillRecord]
    before: list[SignalRecord]
    sources: KillSources


def roles_in(directory: Path) -> dict[int, str]:
    """Every pid this run is known to have had, by the name it goes by here."""
    roles: dict[int, str] = {}
    try:
        record = json.loads((directory / "owner.json").read_text(encoding="utf-8"))
        if isinstance(record, dict) and isinstance(record.get("pid"), int):
            roles[int(record["pid"])] = CONTROLLER
    except (OSError, ValueError):
        pass
    for state in sorted(directory.glob("*.state")):
        pid = read_state(state, None).get("pid")
        if isinstance(pid, int):
            roles[pid] = state.stem
    return roles


def attribute(
    sources: Sources,
    *,
    pid: Optional[int],
    exit_status: Optional[int],
    started_at: Optional[float],
    died_at: float,
) -> Attribution:
    """Everything the sources can say about one death.

    ``started_at`` bounds the search on the early side, so a kill of a
    recycled pid from before this worker existed is never read as this
    worker's. ``died_at`` is when the death was noticed, which is after the
    death itself.
    """
    roles = sources.run_pids() if sources.run_pids is not None else roles_in(sources.directory)
    since = (started_at - 1.0) if started_at else None
    until = died_at + AFTER_DEATH_SECONDS

    killer, before = _from_trace(sources, roles, pid, exit_status, since, until, died_at)
    before += _from_witness(sources, roles, died_at)
    before.sort(key=lambda record: record.at or 0.0)
    if killer is None and pid is not None and roles.get(pid) == CONTROLLER:
        # The process being explained *is* the controller, so the SIGTERM it
        # witnessed itself receiving is not context for somebody else's
        # death: it is the signal that ended it.
        for record in reversed(before):
            if record.target == CONTROLLER and record.source == "controller-witness":
                killer = record
                before.remove(record)
                break
    oom, kernel_status = _from_kernel_log(sources, roles, pid, since, until, died_at, killer)
    return Attribution(
        killer=killer,
        oom=oom,
        before=before,
        sources=KillSources(
            kernel_log=kernel_status,
            signal_trace=sources.trace_status,
            controller_witness=sources.witness_status,
        ),
    )


# -- the tracepoint ---------------------------------------------------------


def _from_trace(
    sources: Sources,
    roles: dict[int, str],
    pid: Optional[int],
    exit_status: Optional[int],
    since: Optional[float],
    until: float,
    died_at: float,
) -> tuple[Optional[SignalRecord], list[SignalRecord]]:
    if pid is None or not sources.trace_path.exists():
        return None, []
    fatal = -exit_status if exit_status is not None and exit_status < 0 else None

    def ready(witness: signal_trace.Witness) -> bool:
        if not witness.delivered or witness.target_pid != pid:
            return False
        if witness.at is not None and (witness.at > until or (since and witness.at < since)):
            return False
        return (witness.via == "TerminateProcess"
                or (exit_status is None and witness.signal == 9)
                or (fatal is not None and witness.signal == fatal))

    records: list[SignalRecord] = []
    for witness in _settled(sources, pid, ready):
        if not witness.delivered:
            continue
        if witness.at is not None and (witness.at > until or (since and witness.at < since)):
            continue
        if witness.target_pid == pid:
            target = "this worker"
        elif roles.get(witness.target_pid) == CONTROLLER:
            target = CONTROLLER
        else:
            continue
        records.append(_record(witness, roles, target, died_at, pid))
    mine = [record for record in records if record.target == "this worker"]
    killer: Optional[SignalRecord] = None
    for record in reversed(mine):
        if record.name == "TerminateProcess":
            # ETW confirms API success; only the parent knows the exit code.
            record.exit_code = exit_status & 0xFFFFFFFF if exit_status is not None else None
            killer = record
            break
        elif (exit_status is None and record.signal == 9) or (fatal is not None and record.signal == fatal):
            killer = record
            break
    before = [record for record in records if record is not killer
              and record.at is not None and died_at - TERM_WINDOW_SECONDS <= record.at <= died_at]
    return killer, before


def _settled(
    sources: Sources, pid: int, ready: Optional[Callable[[signal_trace.Witness], bool]] = None,
) -> list[signal_trace.Witness]:
    """The trace, once the line for this pid has had time to land.

    Polled by size rather than re-parsed: the file holds one line per SIGKILL
    or SIGTERM on the whole machine and is read to a 16 MB tail, so parsing it
    twenty times a second while waiting - and again for every worker of a
    cascade - costs more than the wait it is measuring. Nothing new can have
    arrived while the size is unchanged.
    """
    found = sources.trace_reading()
    # A rejected call, old PID reuse, or a different signal is not the event
    # we are waiting for. Such a row must not end settling before the actual
    # termination reaches the file.
    ready = ready or (lambda witness: witness.target_pid == pid and witness.delivered)
    if (any(ready(witness) for witness in found) or not sources.live
            or time.monotonic() - sources._trace_waited_at < 2.0):
        return found
    deadline = time.monotonic() + TRACE_SETTLE_SECONDS
    size = _size_of(sources.trace_path)
    while not any(ready(witness) for witness in found) and time.monotonic() < deadline:
        time.sleep(TRACE_POLL_SECONDS)
        grown = _size_of(sources.trace_path)
        if grown == size:
            continue
        size = grown
        found = sources.trace_reading()
    # Share the completed wait. Dating the cooldown from its start makes
    # Windows' two-second wait expire its own two-second cooldown, so every
    # death in a cascade pays again.
    sources._trace_waited_at = time.monotonic()
    return found


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _record(
    witness: signal_trace.Witness,
    roles: dict[int, str],
    target: str,
    died_at: float,
    pid: int,
) -> SignalRecord:
    if witness.from_kernel:
        origin = "kernel"
    elif witness.sender_pid == pid:
        origin = "self"
    else:
        origin = "process"
    return SignalRecord(
        signal=witness.signal,
        name="TerminateProcess" if witness.via == "TerminateProcess" else _name(witness.signal),
        exit_code=witness.exit_code,
        api_status=witness.api_status,
        at=witness.at,
        seconds_before_death=_before(witness.at, died_at),
        si_code=witness.si_code,
        origin=origin,
        sender_pid=witness.sender_pid,
        sender_comm=witness.sender_comm,
        sender_cmdline=witness.sender_cmdline,
        sender_exe=witness.sender_exe,
        sender_role=role_of(witness.sender_pid, pid, roles),
        target=target,
        source="signal-trace",
    )


def role_of(sender: Optional[int], pid: Optional[int], roles: dict[int, str]) -> Optional[str]:
    if sender is None:
        return None
    if sender == pid:
        return "itself"
    known = roles.get(sender)
    if known == CONTROLLER:
        return "this run's controller"
    if known:
        return f"{known}, another process of this run"
    return "outside this run"


# -- the controller's own siginfo -------------------------------------------


def _from_witness(
    sources: Sources, roles: dict[int, str], died_at: float
) -> list[SignalRecord]:
    if not sources.witness_path.exists():
        return []
    found: list[SignalRecord] = []
    for event in event_log.read_events(sources.witness_path):
        if event.get("event") != controller_signals.EVENT:
            continue
        at = event.get("time")
        if not isinstance(at, (int, float)):
            continue
        if at > died_at + AFTER_DEATH_SECONDS or at < died_at - TERM_WINDOW_SECONDS:
            continue
        sender = event.get("sender_pid")
        found.append(
            SignalRecord(
                signal=int(event.get("signal") or 0),
                name=str(event.get("name") or _name(int(event.get("signal") or 0))),
                at=float(at),
                seconds_before_death=_before(float(at), died_at),
                si_code=event.get("si_code"),
                origin=str(event.get("origin") or "unknown"),
                sender_pid=sender if isinstance(sender, int) else None,
                sender_uid=event.get("sender_uid"),
                sender_comm=event.get("sender_comm"),
                sender_cmdline=event.get("sender_cmdline"),
                sender_role=role_of(sender if isinstance(sender, int) else None, None, roles),
                target=CONTROLLER,
                source="controller-witness",
            )
        )
    return found


# -- the kernel log ---------------------------------------------------------


def _from_kernel_log(
    sources: Sources,
    roles: dict[int, str],
    pid: Optional[int],
    since: Optional[float],
    until: float,
    died_at: float,
    killer: Optional[SignalRecord],
) -> tuple[Optional[OomKillRecord], str]:
    reading = sources.kernel_log_reading(since, died_at)
    if reading.source == "unavailable":
        return None, f"unavailable ({reading.detail})"
    status = f"{reading.source} ({reading.detail})"
    match: Optional[kernel_log.OomKill] = None
    matched_by = "pid"
    if pid is not None:
        for kill in reading.kills:
            if (kill.victim_pid == pid and kill.at is not None
                    and (since is None or kill.at >= since) and kill.at <= until):
                match = kill
    # A cgroup/time match does not identify a process in another PID
    # namespace. Do not promote another victim's kill to this worker's death.
    if match is None:
        return None, status
    return _oom_record(match, matched_by, roles, pid, status, killer), status


def _oom_record(
    kill: kernel_log.OomKill,
    matched_by: str,
    roles: dict[int, str],
    pid: Optional[int],
    source: str,
    killer: Optional[SignalRecord],
) -> OomKillRecord:
    page = kernel_log.page_kb()

    def megabytes(pages: int) -> int:
        return round(pages * page / 1024)

    # Over the whole table the kernel weighed, not the heaviest few kept for
    # the reader: `tasks_considered` and `tasks_rss_mb` below come from the
    # whole table, and figures drawn from two different populations cannot be
    # set beside each other. A run's smaller workers are exactly what falls
    # off the trimmed end, which would leave `run_tasks` short, the median
    # too high, and `pressure` reading "fleet" for a victim that outgrew one.
    weighed = kill.all_tasks or kill.tasks
    ours = [task for task in weighed if task.pid in roles]
    victim_pages = next((task.rss_pages for task in weighed if task.pid == kill.victim_pid), None)
    rank = None
    if victim_pages is not None:
        rank = 1 + sum(1 for task in weighed if task.rss_pages > victim_pages)
    median = statistics.median(task.rss_pages for task in ours) if ours else None
    pressure: Optional[str] = None
    if len(ours) >= 2 and victim_pages is not None and median:
        pressure = "fleet" if victim_pages <= 1.5 * median else "own weight"
    elif ours:
        pressure = "single"
    record = OomKillRecord(
        victim_pid=kill.victim_pid,
        victim_comm=kill.victim_comm,
        at=kill.at,
        matched_by=matched_by,
        constraint=kill.constraint,
        memcg=kill.memcg,
        task_memcg=kill.task_memcg,
        anon_rss_mb=_kb_to_mb(kill.anon_rss_kb),
        file_rss_mb=_kb_to_mb(kill.file_rss_kb),
        shmem_rss_mb=_kb_to_mb(kill.shmem_rss_kb),
        total_vm_mb=_kb_to_mb(kill.total_vm_kb),
        uid=kill.uid,
        oom_score_adj=kill.oom_score_adj,
        source=source,
        tasks_considered=kill.tasks_considered or None,
        tasks_rss_mb=megabytes(kill.tasks_rss_pages) if kill.tasks_considered else None,
        run_tasks=len(ours) if kill.tasks_considered else None,
        run_rss_mb=megabytes(sum(task.rss_pages for task in ours)) if ours else None,
        run_median_rss_mb=megabytes(int(median)) if median is not None else None,
        victim_rank=rank,
        largest=[
            {
                "pid": task.pid,
                "name": task.name,
                "rss_mb": megabytes(task.rss_pages),
                "role": roles.get(task.pid),
            }
            for task in kill.tasks[:3]
        ],
        pressure=pressure,
    )
    if killer is not None and killer.origin == "kernel":
        record.triggered_by_pid = killer.sender_pid
        record.triggered_by_comm = killer.sender_comm
        record.triggered_by_role = role_of(killer.sender_pid, pid, roles)
    return record


def _kb_to_mb(value: Optional[int]) -> Optional[int]:
    return None if value is None else round(value / 1024)


def _before(at: Optional[float], died_at: float) -> Optional[float]:
    if at is None:
        return None
    return round(max(0.0, died_at - at), 1)


def _name(number: int) -> str:
    try:
        return signal_module.Signals(number).name
    except ValueError:
        return f"signal {number}"


def controller_pid() -> int:
    return os.getpid()
