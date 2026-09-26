"""Thread lanes: several workers in one process, and how their files say so.

pytest-threadlanes runs xdist's own schedulers on threads it calls lanes. A
lane is an xdist worker in every way a plugin can observe - ``workerinput``
names it on its own thread, its reports carry ``report.lane_id``, and in a run
with no ``-n`` it is a node for xdist's controller hooks - except the one this
package was built on: it shares its process with its siblings. One process is
no longer one worker with at most one test in flight.

So a lane is recorded as a worker. It gets a state slot of its own, named after
it (``ln3.state``, ``gw0.ln3.state``), beside its process's files, and every
reader that enumerates ``*.state`` sees it as a worker without being taught
about lanes. What is truly per process - the heartbeat, the resident memory,
the pid, the crash and watchdog dumps - stays in the process's files, and a
lane's record names its process so a reader can find them. The process's own
record says ``"lanes": true`` once its first lane has started a test: from
then on it is a container, not a worker, and its row is its lanes.

Nothing here imports pytest-threadlanes, and nothing needs it installed. A run
is told it has lanes by the option the plugin registers, which is simply absent
when it is not; and a reader is told by the files, which a run without lanes
never writes those keys into. Every path through here is therefore the path a
run without lanes has always taken, unless a lane is actually running.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

#: A thread to pick out of a stack: its ident as faulthandler prints it, and
#: its name as a live read prints it. Either may be unknown.
ThreadKey = tuple[Optional[int], Optional[str]]

#: pytest-threadlanes' plugin name, as its entry point registers it.
PLUGIN = "threadlanes"

#: The key a lane's record carries naming the worker process it runs in:
#: ``main`` in a run with no ``-n``, ``gw0`` in one with.
PROCESS_KEY = "process"

#: The key a process's own record carries once it has lanes. Its row is then
#: its lanes, and it is read as the container it is.
CONTAINER_KEY = "lanes"


def requested(config: Any) -> bool:
    """Whether this run asked pytest-threadlanes for lanes.

    Both halves, because either alone is somebody else's: an option whose
    dest is ``lanes`` can be any plugin's or conftest's, and pytest-threadlanes
    installed does nothing until asked. ``getoption`` with a default, because
    the option exists only where the plugin is installed - and ``--lanes 0``
    is its spelling of "off".
    """
    try:
        return bool(
            config.pluginmanager.hasplugin(PLUGIN) and config.getoption("lanes", None)
        )
    except (ValueError, AttributeError, TypeError):
        return False


def in_one_process(config: Any) -> bool:
    """Whether this run's lanes all live in this process.

    That is ``--lanes N`` without ``-n`` or ``--tx``, whatever ``--dist`` says:
    pytest-threadlanes then plays xdist's controller and its workers on this
    process's threads, and xdist itself starts nothing. A ``--dist`` given
    alongside only chooses the scheduler, so the run records here as a run
    with no workers does, rather than waiting for workers that never come.
    """
    if not requested(config) or hasattr(config, "workerinput"):
        return False
    try:
        return not config.getoption("tx", None) and not config.getoption(
            "numprocesses", None
        )
    except (ValueError, AttributeError, TypeError):
        return False


def without_unset(dumped: Any, model: Any, names: tuple[str, ...]) -> Any:
    """A model's dump without the lane-only fields it left unset.

    What a payload the plugin produces carries only where lanes ran: absent,
    not null or empty, everywhere else, so a run without lanes serves exactly
    what it did before lanes existed. For the wrap serializer of each model
    that has such fields - which must not annotate its return, or pydantic
    reads the model's serialization schema as "anything".
    """
    if isinstance(dumped, dict):
        for name in names:
            value = getattr(model, name, None)
            if value is None or (isinstance(value, (list, dict)) and not value):
                dumped.pop(name, None)
    return dumped


def is_lane(record: dict[str, Any]) -> bool:
    """Whether a state record was written by a lane."""
    return isinstance(record.get(PROCESS_KEY), str) and bool(record.get(PROCESS_KEY))


def is_container(record: dict[str, Any]) -> bool:
    """Whether a state record is a process whose lanes are its workers."""
    return record.get(CONTAINER_KEY) is True


def sibling(path: Path, name: Any, suffix: str) -> Optional[Path]:
    """``<name><suffix>`` beside ``path``, or None when ``name`` is not a name.

    The name comes out of a state file, and a state file is something a
    process wrote rather than something this reader chose. One that says its
    process is ``../../x`` must not become a path outside the run, so anything
    that is not a single plain component is refused rather than joined.
    """
    if not isinstance(name, str) or not name or name in (".", ".."):
        return None
    if "/" in name or "\\" in name or "\x00" in name:
        return None
    return path.with_name(f"{name}{suffix}")


def lane_records(
    directory: Path, process: str, run_id: Optional[str] = None
) -> list[tuple[str, dict[str, Any]]]:
    """Every lane of ``process`` in this run, as ``(lane, record)``, by name.

    Found by listing the directory and reading what each state says it
    belongs to, never by building a lane's name: a lane is whatever the
    process called it, and the listing is the only authority on what exists.
    """
    from .capture.state import read_state

    found = []
    try:
        states = sorted(directory.glob("*.state"))
    except OSError:
        return []
    for path in states:
        if path.stem == process:
            continue
        record = read_state(path, run_id)
        if is_lane(record) and record.get(PROCESS_KEY) == process:
            found.append((path.stem, record))
    return found


def in_flight(
    lanes: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """The lanes above that had a test running, in the shape an incident lists
    them: which lane, which test, and in which phase."""
    return [
        {
            "lane": lane,
            "nodeid": record.get("nodeid"),
            "nodeid_hash": record.get("nodeid_hash"),
            "phase": record.get("phase"),
        }
        for lane, record in lanes
        if record.get("nodeid")
    ]


#: Where a process of lanes keeps its lanes' own CPU readings, beside its
#: event log: ``<process>.lanecpu``.
LANE_CPU_SUFFIX = ".lanecpu"

#: How many readings of each lane the file keeps: a rate needs two, and a
#: stall verdict measures over the beats of its silence.
LANE_CPU_READINGS = 6


class LaneCpu:
    """One process's lanes' thread CPU, a reading per lane at every beat.

    One file per process, rewritten whole at each beat, rather than a reading
    on each lane's own record: every lane is then read at the same instant,
    so two lanes compared are compared over the same window, and a beat
    costs the heartbeat one write however many lanes there are - fifty
    writes a beat, one per lane, pushed the heartbeat itself seconds late.
    And not on the beat's own line, which then grew with the lane count:
    fourteen kilobytes a beat at a thousand lanes, which pushed all but a
    handful of beats out of the tail a reader takes.

    Written to a temporary name and moved into place, so a reader sees one
    whole reading or the one before it. Not thread-safe: the heartbeat's
    thread is the only writer.
    """

    def __init__(self, path: Path, run_id: Optional[str]) -> None:
        self.path = path
        self.run_id = run_id
        self._times: list[float] = []
        self._lanes: dict[str, list[Optional[float]]] = {}

    def record(self, stamp: float, used: dict[str, float]) -> None:
        """One reading of every lane measured now; a lane not in ``used``
        was not, and reads as a gap."""
        self._times.append(round(stamp, 3))
        for lane in used:
            self._lanes.setdefault(lane, [None] * (len(self._times) - 1))
        for lane, readings in self._lanes.items():
            value = used.get(lane)
            readings.append(None if value is None else round(value, 3))
        keep = LANE_CPU_READINGS
        self._times = self._times[-keep:]
        self._lanes = {
            lane: readings[-keep:]
            for lane, readings in self._lanes.items()
            if any(value is not None for value in readings[-keep:])
        }
        staging = self.path.with_name(self.path.name + ".part")
        try:
            staging.write_text(
                json.dumps({"run_id": self.run_id, "times": self._times, "lanes": self._lanes}),
                encoding="utf-8",
            )
            os.replace(staging, self.path)
        except OSError:
            pass  # a reading lost is a rate one beat older, never a failed run


def lane_cpu(
    directory: Path, process: Any, run_id: Optional[str] = None
) -> dict[str, list[list[float]]]:
    """Each lane of ``process``'s ``[time, seconds]`` readings, oldest first.

    Empty where there are none - no lanes, a platform that cannot number
    threads, or another run's file.
    """
    path = sibling(directory / "x", process, LANE_CPU_SUFFIX)
    if path is None:
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    written_by = loaded.get("run_id")
    if run_id and written_by and written_by != run_id:
        return {}
    times = loaded.get("times")
    lanes = loaded.get("lanes")
    if not isinstance(times, list) or not isinstance(lanes, dict):
        return {}
    found: dict[str, list[list[float]]] = {}
    for lane, values in lanes.items():
        if not isinstance(values, list):
            continue
        found[str(lane)] = [
            [float(stamp), float(value)]
            for stamp, value in zip(times[-len(values):], values)
            if isinstance(stamp, (int, float)) and isinstance(value, (int, float))
        ]
    return found
