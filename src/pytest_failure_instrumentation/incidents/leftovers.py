"""What a run that never came back left behind, and who reports it.

Every other kind is raised by a process that survived to raise it. A run that
was killed has no survivor. A plain ``pytest`` that segfaults, a controller
reclaimed with its workers, a CI job cancelled mid-suite: whatever those runs
recorded is on disk and complete, and there is nobody left in them to read it.
Under xdist a worker's death at least has a controller watching; the run's own
death never does, by definition.

So it is reported by the next run to use the directory - which was already
walking it. A starting run sweeps the directories of runs that are over before
making its own, and the sweep and the report are one walk over one marker:

*Is this run over?* Its owner pid is in the marker, so a run still going is
recognisable however long it has been going. That matters because several run
at once, and deleting a live run's evidence is how a cleanup once broke the
reports it existed to produce.

*Did it report for itself?* A run that reached session finish stamps the
marker on its way out. It raised its own incidents - a worker death, a stall,
an internal error, a summary - and re-reporting them a day later against
whichever run happened to notice is worse than never reporting them at all.

Only a run that is over *and* never stamped that mark has anything here to
report, which is exactly the set of runs that could not report for themselves.

Two runs starting at the same moment can both find the same corpse, since the
directory is only removed after it has been read. That is a duplicate rather
than a wrong answer: both carry the dead run's own ``run_id`` and the same
fingerprint, which is what a consumer already groups recurrences by.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .. import probes

if TYPE_CHECKING:
    from .death import WorkerDeathIncident

#: Written at the top of each run's own directory, and the only thing that
#: makes a directory this plugin's to read or to delete. Matching on file
#: suffixes instead is how a cleanup takes somebody's coverage report with it:
#: ``failure_directory`` is a natural thing to point at an existing artifacts
#: directory.
OWNER_FILE = "owner.json"

#: The key a run stamps into its marker at session finish. Its *absence* is
#: the finding - see the module docstring - which is why it is spelled once
#: here rather than at each of the two ends.
FINISHED_KEY = "finished_at"
#: The key the sidecar's reporter stamps once it has raised a killed run's
#: incidents itself - see :mod:`.reporter`. A directory carrying it has been
#: reported, and a later run must not raise it a second time.
REPORTED_KEY = "reported_at"


def marker(directory: Path) -> Optional[dict[str, Any]]:
    """This run's marker, or None if this directory is not one of ours."""
    try:
        record = json.loads((directory / OWNER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def owner_of(directory: Path) -> Optional[int]:
    """The pid that owns this run directory, or None if it is not ours."""
    record = marker(directory)
    pid = record.get("pid") if record else None
    return int(pid) if isinstance(pid, int) else None


def stamp(directory: Path, key: str) -> bool:
    """Add ``key`` (the time, now) to this directory's marker.

    Written through a temporary file and renamed over the marker, because the
    readers of it are other runs: :func:`marker` on a half-written owner.json
    returns None, and a directory that reads as "not ours" is one whose
    incidents are never raised at all. The rename is atomic, so a reader sees
    the old marker or the new one.
    """
    record = marker(directory)
    if record is None:
        return False
    record[key] = time.time()
    temporary = directory / f"{OWNER_FILE}.{os.getpid()}"
    try:
        temporary.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temporary, directory / OWNER_FILE)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return False
    return True


def run_directories(root: Path) -> list[Path]:
    try:
        return sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return []


def prune_finished_runs(root: Path) -> None:
    """Delete the directories of runs that are over.

    Over, not old. The owner's pid is in the marker, so a run that is still
    going is recognisable as such however long it has been going - which
    matters, because the whole reason each run has a directory is that several
    of them happen at once.

    A directory without our marker is not ours and is left alone, whatever it
    looks like.

    Called after :func:`deaths_left_behind`, never before it: this is what
    removes the evidence that function reads, and the two orders differ by
    whether a killed run is reported once or not at all.
    """
    for path in run_directories(root):
        owner = owner_of(path)
        if owner is None or probes.is_running(owner):
            continue
        shutil.rmtree(path, ignore_errors=True)


def deaths_left_behind(
    root: Path, mine: Optional[Path] = None, elevate: bool = False
) -> list[WorkerDeathIncident]:
    """One incident per process of a run that ended without reporting.

    ``elevate`` is the reporting run's own permission to spend ``sudo`` on
    the kernel log, which is the one witness a dead run can still be asked
    about: the log is machine-wide and timestamped, so a kill from the dead
    run's window is the dead run's.

    ``mine`` is this run's own directory, skipped whatever state it is in.
    Nothing else would match it - a live run's marker names a live pid, and at
    the moment this is called ours has no marker at all - but a report by this
    run about this run is a wrong answer bad enough to be worth two lines to
    make impossible rather than merely unlikely.

    Nothing here raises. A directory this cannot make sense of yields nothing
    and the next one is read, because a starting run must not be stopped by
    the remains of one that already failed.
    """
    found: list[WorkerDeathIncident] = []
    for directory in run_directories(root):
        if mine is not None and directory == mine:
            continue
        try:
            found.extend(_deaths_in(directory, elevate))
        except Exception:  # noqa: BLE001 - see the docstring
            continue
    return found


def deaths_of(directory: Path, elevate: bool = False) -> list[WorkerDeathIncident]:
    """Every death in one run directory that is over and never reported.

    The same reading :func:`deaths_left_behind` makes for each directory,
    offered on its own for the sidecar's reporter, which knows which
    directory it is asking about.
    """
    return _deaths_in(directory, elevate)


def _deaths_in(directory: Path, elevate: bool = False) -> list[WorkerDeathIncident]:
    record = marker(directory)
    if record is None:
        return []  # not ours
    owner = record.get("pid")
    if not isinstance(owner, int) or probes.is_running(owner):
        return []  # still going, so not yet anybody's to report
    if record.get(FINISHED_KEY):
        return []  # it reached its own session finish and reported for itself
    if record.get(REPORTED_KEY):
        return []  # the sidecar's reporter raised its incidents already

    # The run is over, so every process in it is over: nothing needs asking
    # about the individual pids, and asking would make it worse. A pid the
    # kernel has since handed to something else would answer "alive" and
    # suppress a real report - which is the failure mode that costs a reader
    # the one incident they were waiting for.
    from . import death

    incidents = []
    for events in sorted(directory.glob("*.events")):
        incident = death.recover(events, session=directory.name, elevate=elevate)
        if incident is not None:
            incidents.append(incident)
    # The controller last, and separately: it keeps no event log of the
    # shape above, and a cancelled job is a controller that died while its
    # workers went on to finish cleanly - see death.recover_controller.
    controller = death.recover_controller(
        directory, session=directory.name, marker=record, elevate=elevate
    )
    if controller is not None:
        incidents.append(controller)
    return incidents
