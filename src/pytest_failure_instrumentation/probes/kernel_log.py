"""The kernel's own account of an OOM kill, read back out of its log.

The cgroup counter says *that* something in the cgroup was killed by the OOM
killer. The kernel log says *what*, and it is the only source that does: one
``Out of memory: Killed process 4242 (python) ...`` line per kill naming the
pid, its resident size as the kernel saw it, the constraint (a cgroup's limit
or the machine's), the memcg the victim lived in - and, when
``vm.oom_dump_tasks`` is on, which it is by default, a table of every task the
killer considered with its own RSS. For a run of a hundred workers that table
is the whole fleet at the instant the decision was made, printed by the thing
that made it.

Reading it is the per-distro part, and every rung of the ladder below is tried
in order, with the one that answered recorded rather than assumed:

``/dev/kmsg``
    The ring buffer itself. Open to everyone where ``kernel.dmesg_restrict`` is
    0, and only to ``CAP_SYSLOG`` where it is 1 - which Ubuntu since 20.04 and
    Fedora set by default. Inside a container it is usually the host's buffer
    or nothing.
``journalctl -k``
    The same lines, kept by systemd. Readable by members of ``adm`` or
    ``systemd-journal``, which Ubuntu grants its first user.
``dmesg``, then ``sudo -n dmesg``
    The same buffer through a tool, and then through a tool with a privilege
    this run has been allowed to spend. ``-n`` means a sudo that would prompt
    fails instead, so an unattended run never hangs on a password.

What comes back is the *matching* record, never the fact that the log holds
one: a machine running many suites has many kills in its log, and the pid, the
time and the cgroup are what tie one of them to one of ours. Inside a pid
namespace the pid the kernel prints is the host's and matches nothing here, so
a matching cgroup alone is insufficient to name this process as the victim.

Nothing here raises, and nothing here reports a value without saying which
rung produced it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .platform_flags import IS_LINUX

KMSG = "/dev/kmsg"

#: One record from ``/dev/kmsg``: ``prio,seq,usec,flags;message``, and then
#: optional continuation lines carrying key=value pairs, which are not part
#: of the message.
_KMSG_RECORD = re.compile(r"^(?P<prio>\d+),(?P<seq>\d+),(?P<usec>\d+),[^;]*;(?P<message>.*)$")

#: ``dmesg``'s default line: ``[  401.375501] message``. The number is
#: seconds since boot on the kernel's own clock.
_DMESG_LINE = re.compile(r"^\[\s*(?P<seconds>\d+\.\d+)\]\s?(?P<message>.*)$")

#: The line every kernel since 2.6 prints for a kill, whatever preceded it:
#: ``Out of memory: Killed process 4242 (python3) total-vm:...`` on 5.x, and
#: ``Killed process 4242 (python3) total-vm:...`` on 4.x after a separate
#: ``Kill process ... score ... or sacrifice child`` line.
_KILLED = re.compile(r"Killed process (?P<pid>\d+) \((?P<comm>[^)]*)\)(?P<rest>.*)")
_FIELD = re.compile(r"(?P<key>total-vm|anon-rss|file-rss|shmem-rss):(?P<value>\d+)kB")
_UID = re.compile(r"\bUID:(?P<uid>\d+)")
_SCORE_ADJ = re.compile(r"oom_score_adj:(?P<adj>-?\d+)")

#: The summary line 4.19 and later print just before the kill, and the only
#: place the constraint and the cgroup are stated:
#: ``oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/,
#: mems_allowed=0,oom_memcg=/docker/abc,task_memcg=/docker/abc,task=python3,
#: pid=4242,uid=1000``. Parsed as key=value pairs rather than by position,
#: because ``global_oom`` sits in the middle with no value at all.
_OOM_SUMMARY = re.compile(r"oom-kill:(?P<pairs>.*)$")

#: The task table: a header and then one row per task the killer weighed.
#: Column sets differ by kernel (``nr_ptes nr_pmds`` on 4.x, ``pgtables_bytes``
#: on 5.x) but the first five and the last two are stable: pid, uid, tgid,
#: total_vm, rss ... oom_score_adj, name.
_TASK_HEADER = re.compile(r"\[\s*pid\s*\]\s+uid\s+tgid\s+total_vm\s+rss\b")
_TASK_ROW = re.compile(
    r"\[\s*(?P<pid>\d+)\]\s+(?P<uid>\d+)\s+(?P<tgid>\d+)\s+(?P<total_vm>\d+)\s+"
    r"(?P<rss>\d+)\s+(?P<rest>.+)$"
)

#: How many task rows a kill keeps. A machine with ten thousand tasks would
#: otherwise put ten thousand rows on an incident; what the fleet summary
#: needs is every row for the *arithmetic*, which is done before this bound
#: applies, and only the largest few for the reader.
KEPT_TASKS = 400


@dataclass
class OomTask:
    """One row of the killer's table, in the kernel's own units (pages)."""

    pid: int
    uid: int
    total_vm_pages: int
    rss_pages: int
    oom_score_adj: Optional[int]
    name: str


@dataclass
class OomKill:
    victim_pid: int
    victim_comm: str
    #: Wall-clock seconds, when the log carried a timestamp that could be
    #: placed; None otherwise.
    at: Optional[float]
    #: ``CONSTRAINT_NONE`` for the machine's memory, ``CONSTRAINT_MEMCG`` for a
    #: cgroup's limit, ``CONSTRAINT_CPUSET`` / ``CONSTRAINT_MEMORY_POLICY``
    #: for a NUMA policy. None where the kernel was too old to say.
    constraint: Optional[str]
    #: The cgroup whose limit was hit, and the cgroup the victim lived in.
    #: They differ when the killer reaches into a child.
    memcg: Optional[str]
    task_memcg: Optional[str]
    total_vm_kb: Optional[int]
    anon_rss_kb: Optional[int]
    file_rss_kb: Optional[int]
    shmem_rss_kb: Optional[int]
    uid: Optional[int]
    oom_score_adj: Optional[int]
    #: The heaviest ``KEPT_TASKS`` rows, for the reader: what goes on the
    #: incident is the top few of these.
    tasks: list[OomTask] = field(default_factory=list)
    #: Every row the table held, for the arithmetic. The fleet figures - how
    #: many of the run's processes the kernel weighed, their median size, the
    #: victim's rank among all of them - are only true over the whole table,
    #: and which rows belong to the run is not known here: a worker holding
    #: less memory than four hundred other tasks is still one of ours, and
    #: dropping it makes the run look both smaller and heavier than it was.
    #: The parse materialises the whole table anyway, so keeping it costs no
    #: peak memory; the object is dropped once the record is built.
    all_tasks: list[OomTask] = field(default_factory=list)
    #: The rows the table held, and the sum of their RSS.
    tasks_considered: int = 0
    tasks_rss_pages: int = 0
    lines: list[str] = field(default_factory=list)


@dataclass
class KernelLogReading:
    kills: list[OomKill]
    #: Which rung answered: ``kmsg``, ``journal``, ``dmesg``, ``sudo dmesg`` -
    #: or ``unavailable``.
    source: str
    #: Why not, when it is ``unavailable``; otherwise how many lines were read.
    detail: str


# -- reading ----------------------------------------------------------------


def read(since: Optional[float] = None, elevate: bool = False) -> KernelLogReading:
    """Every OOM kill the log holds from ``since`` (wall-clock) on.

    ``elevate`` allows the last rung, ``sudo -n dmesg``. Nothing is elevated
    without it, and nothing prompts with it.
    """
    if not IS_LINUX:
        return KernelLogReading([], "unavailable", "the kernel log is a Linux source")
    refusals: list[str] = []
    for name, reader in (
        ("kmsg", _read_kmsg),
        ("journal", lambda: _read_journal(since)),
        ("dmesg", _read_dmesg),
    ):
        lines, why = reader()
        if lines is not None:
            return KernelLogReading(_since(parse(lines), since), name, f"{len(lines)} lines")
        refusals.append(f"{name}: {why}")
    if elevate:
        lines, why = _read_dmesg(sudo=True)
        if lines is not None:
            return KernelLogReading(
                _since(parse(lines), since), "sudo dmesg", f"{len(lines)} lines"
            )
        refusals.append(f"sudo dmesg: {why}")
    else:
        refusals.append("sudo dmesg: not tried, failure_elevate is off")
    return KernelLogReading([], "unavailable", "; ".join(refusals))


def narrowed(reading: KernelLogReading, since: Optional[float]) -> KernelLogReading:
    """The same reading, bounded to a later window.

    A reading taken for one process of a run serves every other process of it,
    which started later: the kills are the same lines, and the only difference
    is where the window opens. Narrowing an existing reading is what makes one
    read of the log answer for a whole cascade of deaths - see
    ``killer.Sources.kernel_log_reading``.
    """
    return KernelLogReading(_since(reading.kills, since), reading.source, reading.detail)


def _since(kills: list[OomKill], since: Optional[float]) -> list[OomKill]:
    if since is None:
        return kills
    # A kill with no timestamp cannot be placed and is kept: the pid match
    # downstream is the stronger key, and dropping it here would hide a kill
    # the reader could still have tied to a worker.
    return [kill for kill in kills if kill.at is None or kill.at >= since - 1.0]


def _boot_epoch() -> float:
    """When the kernel's clock read zero, on the wall clock.

    printk stamps with ``local_clock()``, which tracks CLOCK_MONOTONIC on
    every machine this runs on; the difference between that and the wall
    clock, read now, is the offset. It drifts across a suspend, which no CI
    machine does mid-run.
    """
    return time.time() - time.clock_gettime(time.CLOCK_MONOTONIC)


def _read_kmsg() -> tuple[Optional[list[tuple[Optional[float], str]]], str]:
    """The ring buffer, straight from the device."""
    try:
        fd = os.open(KMSG, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as failure:
        return None, _why(failure)
    boot = _boot_epoch()
    lines: list[tuple[Optional[float], str]] = []
    try:
        deadline = time.monotonic() + 0.5
        while len(lines) < 10000 and time.monotonic() < deadline:
            try:
                raw = os.read(fd, 8192)
            except BlockingIOError:
                break  # caught up
            except OSError as failure:
                # EPIPE means the buffer overwrote records under us; keep
                # going. Anything else - EPERM on a restricted machine is
                # raised here rather than at open on some kernels - ends it.
                if failure.errno == 32:
                    continue
                if not lines:
                    return None, _why(failure)
                break
            if not raw:
                break
            match = _KMSG_RECORD.match(raw.decode("utf-8", "replace").split("\n", 1)[0])
            if match is None:
                continue
            lines.append((boot + int(match.group("usec")) / 1e6, match.group("message")))
    finally:
        os.close(fd)
    if not lines:
        # An open that succeeds and reads nothing is not an empty ring buffer -
        # a fresh open starts at the oldest record the kernel still holds, and
        # a running kernel always holds some. It is /dev/kmsg bound to
        # /dev/null, which systemd-nspawn and a few container runtimes do.
        # Answering "read, 0 lines" would end the ladder here and leave the
        # journal and dmesg untried, so an OOM kill goes unfound while the
        # incident says the log was consulted.
        return None, "the device read as empty, so it is not the kernel's ring buffer"
    return lines, ""


def _bounded_command(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Bound command duration and bytes loaded into Python memory."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        done = subprocess.run(command, stdout=out, stderr=err, timeout=2)
        out.seek(0, 2)
        size = out.tell()
        out.seek(max(0, size - 4 * 1024 * 1024))
        data = out.read(4 * 1024 * 1024)
        if size > 4 * 1024 * 1024:
            data = data.partition(b"\n")[2]
        err.seek(0)
        return subprocess.CompletedProcess(command, done.returncode, data, err.read(4096))


def _read_journal(
    since: Optional[float] = None,
) -> tuple[Optional[list[tuple[Optional[float], str]]], str]:
    """The same lines out of systemd's journal, with its own wall-clock stamp.

    Bounded to the run's window when one is known: a machine's kernel journal
    can be months long, and a death is only ever explained by the last few
    minutes of it.
    """
    command = ["journalctl", "-k", "-o", "json", "--no-pager", "-q", "-n", "10000"]
    if since is not None:
        command.append(f"--since=@{int(since) - 1}")
    try:
        result = _bounded_command(command)
    except (OSError, subprocess.SubprocessError) as failure:
        return None, _why(failure)
    if result.returncode != 0:
        return None, _first_line(result.stderr) or f"exit status {result.returncode}"
    lines: list[tuple[Optional[float], str]] = []
    for raw in result.stdout.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        message = entry.get("MESSAGE")
        if isinstance(message, list):  # bytes the journal could not decode
            message = bytes(byte for byte in message if isinstance(byte, int)).decode(
                "utf-8", "replace"
            )
        if not isinstance(message, str):
            continue
        stamp = entry.get("__REALTIME_TIMESTAMP")
        at = int(stamp) / 1e6 if isinstance(stamp, str) and stamp.isdigit() else None
        lines.append((at, message))
    if not lines:
        # ``journalctl -k`` on a machine with no journal exits 0 and prints
        # nothing, or prints "No journal files were found" to stderr.
        return None, _first_line(result.stderr) or "the journal holds no kernel lines"
    return lines, ""


def _read_dmesg(sudo: bool = False) -> tuple[Optional[list[tuple[Optional[float], str]]], str]:
    command = (["sudo", "-n"] if sudo else []) + ["dmesg"]
    try:
        result = _bounded_command(command)
    except (OSError, subprocess.SubprocessError) as failure:
        return None, _why(failure)
    if result.returncode != 0:
        return None, _first_line(result.stderr) or f"exit status {result.returncode}"
    boot = _boot_epoch()
    lines: list[tuple[Optional[float], str]] = []
    for raw in result.stdout.decode("utf-8", "replace").splitlines():
        match = _DMESG_LINE.match(raw)
        if match is None:
            lines.append((None, raw))  # a dmesg built without timestamps
        else:
            lines.append((boot + float(match.group("seconds")), match.group("message")))
    if not lines:
        # The same as for the device above, and it matters for the same
        # reason: an empty dmesg here would end the ladder before ``sudo
        # dmesg``, which is the rung that answers when the buffer is
        # restricted rather than absent.
        return None, "dmesg printed nothing"
    return lines, ""


def _why(failure: BaseException) -> str:
    if isinstance(failure, PermissionError):
        restricted = _sysctl("kernel.dmesg_restrict")
        note = f" (kernel.dmesg_restrict={restricted})" if restricted else ""
        return f"permission denied{note}"
    if isinstance(failure, FileNotFoundError):
        return "not present"
    return repr(failure)


def _sysctl(name: str) -> Optional[str]:
    try:
        with open("/proc/sys/" + name.replace(".", "/"), encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _first_line(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").strip().splitlines()[0] if raw.strip() else ""


# -- parsing ----------------------------------------------------------------


def parse(lines: list[tuple[Optional[float], str]]) -> list[OomKill]:
    """Every kill in a sequence of kernel lines, each with the table and the
    summary line that preceded it.

    The kernel prints them in a fixed order - the task table, then (on 4.19
    and later) the ``oom-kill:`` summary, then the ``Killed process`` line -
    so a table and a summary belong to the *next* kill line and to nothing
    after it. Two kills back to back each get their own.
    """
    kills: list[OomKill] = []
    table: list[OomTask] = []
    in_table = False
    summary: dict[str, str] = {}
    memcg_prefixed = False
    context: list[str] = []
    for at, message in lines:
        if _TASK_HEADER.search(message):
            table, in_table = [], True
            context = [message]
            continue
        if in_table:
            row = _TASK_ROW.search(message)
            if row is not None:
                table.append(_task(row))
                if len(context) < 4:
                    context.append(message)
                continue
            in_table = False
        found = _OOM_SUMMARY.search(message)
        if found is not None:
            summary = _pairs(found.group("pairs"))
            context.append(message)
            continue
        if "Memory cgroup out of memory" in message:
            memcg_prefixed = True
        killed = _KILLED.search(message)
        if killed is None:
            continue
        context.append(message)
        kills.append(_kill(at, killed, summary, table, memcg_prefixed, context))
        table, summary, memcg_prefixed, context = [], {}, False, []
    return kills


def _task(row: re.Match[str]) -> OomTask:
    rest = row.group("rest").split()
    # ``... swapents oom_score_adj name``: the name is last, the adjustment
    # just before it. A comm with a space in it loses its first word, which
    # is a lesser loss than misreading the adjustment as part of a name.
    name = rest[-1] if rest else ""
    adj: Optional[int] = None
    if len(rest) >= 2:
        try:
            adj = int(rest[-2])
        except ValueError:
            adj = None
    return OomTask(
        pid=int(row.group("pid")),
        uid=int(row.group("uid")),
        total_vm_pages=int(row.group("total_vm")),
        rss_pages=int(row.group("rss")),
        oom_score_adj=adj,
        name=name,
    )


def _pairs(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in text.split(","):
        key, separator, value = item.partition("=")
        if separator:
            pairs[key.strip()] = value.strip()
    return pairs


def _kill(
    at: Optional[float],
    killed: re.Match[str],
    summary: dict[str, str],
    table: list[OomTask],
    memcg_prefixed: bool,
    context: list[str],
) -> OomKill:
    rest = killed.group("rest")
    fields = {match.group("key"): int(match.group("value")) for match in _FIELD.finditer(rest)}
    uid_match = _UID.search(rest)
    adj_match = _SCORE_ADJ.search(rest)
    constraint = summary.get("constraint")
    if constraint is None and memcg_prefixed:
        constraint = "CONSTRAINT_MEMCG"
    uid: Optional[int] = None
    if uid_match is not None:
        uid = int(uid_match.group("uid"))
    elif summary.get("uid", "").isdigit():
        uid = int(summary["uid"])
    return OomKill(
        victim_pid=int(killed.group("pid")),
        victim_comm=killed.group("comm"),
        at=at,
        constraint=constraint,
        memcg=summary.get("oom_memcg"),
        task_memcg=summary.get("task_memcg"),
        total_vm_kb=fields.get("total-vm"),
        anon_rss_kb=fields.get("anon-rss"),
        file_rss_kb=fields.get("file-rss"),
        shmem_rss_kb=fields.get("shmem-rss"),
        uid=uid,
        oom_score_adj=int(adj_match.group("adj")) if adj_match else None,
        tasks=sorted(table, key=lambda task: task.rss_pages, reverse=True)[:KEPT_TASKS],
        all_tasks=table,
        tasks_considered=len(table),
        tasks_rss_pages=sum(task.rss_pages for task in table),
        lines=context[-6:],
    )


def page_kb() -> int:
    """The unit of the table's ``rss`` column."""
    try:
        return max(1, os.sysconf("SC_PAGE_SIZE") // 1024)
    except (ValueError, OSError, AttributeError):
        return 4


def own_cgroup() -> Optional[str]:
    """This process's cgroup path, as the kernel would print it in ``task_memcg``.

    cgroup v2 has one line, ``0::/path``; v1 lists one per controller and the
    memory controller's is the one the OOM killer names.
    """
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as handle:
            entries = handle.read().splitlines()
    except OSError:
        return None
    fallback: Optional[str] = None
    for entry in entries:
        parts = entry.split(":", 2)
        if len(parts) != 3:
            continue
        _hierarchy, controllers, path = parts
        if controllers == "" or "memory" in controllers.split(","):
            return path
        fallback = fallback or path
    return fallback


def describe(reading: KernelLogReading) -> dict[str, Any]:
    """The reading, minus the kills, for a capabilities-style record."""
    return {"source": reading.source, "detail": reading.detail}
