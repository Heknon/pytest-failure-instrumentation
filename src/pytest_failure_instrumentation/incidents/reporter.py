"""Report a killed run from the process that outlived it.

Every incident this package raises is raised by a process that survived to
raise it. A run whose controller is killed - a cancelled job, an OOM kill, a
``taskkill /T`` - has no such process, and until now was reported by the next
run over the same evidence directory. On a CI runner that gets a fresh
workspace for every job there is no next run, and a killed run was a run
about which nothing was ever said.

The sidecar (:mod:`..probes.signal_trace`) can survive controller death, though
a container, cgroup, or host shutdown can kill it too. It holds the
read end of a pipe only the controller can write, so the controller dying,
whatever killed it, is EOF on that pipe. A controller that reaches session
finish says ``stop`` first; EOF without it is a death. The sidecar then
starts *this* module in a child of its own, hands it the payload the
controller sent at startup, and the child builds the same incidents the next
run would have recovered - the controller's own death above all - and calls
the callable the user configured with each one.

**The callable travels as a pickle, and that sets the rules.** A module-level
function, or a ``functools.partial`` of one with picklable bound arguments,
handed to ``install(config, on_run_death=...)`` or named in ini as
``failure_on_run_death = package.module:attribute``. Lambdas, closures and
anything holding the pytest config will not pickle; the controller says so
at session start and the run proceeds without a reporter. A function in the
rootdir's ``conftest.py`` works: the controller's import path travels too.

**Nothing of the user's runs as root.** The Linux sidecar may be root. It
never unpickles anything; it starts this child as the user that started the
run, with that user's groups, environment and working directory. The
environment - which is where alerting tokens live - travels down the two
pipes and never touches disk.

**Nothing here can reach a run.** The run is over. What it can reach is the
sidecar, which waits on this child with a timeout and reads nothing back;
and the next run, which is told by a stamp on the marker that this
directory has already been reported and must not be raised again.
"""

from __future__ import annotations

import base64
import importlib
import json
import pickle
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

#: How long the controller's pid may take to disappear after its pipe closed:
#: a process closes its descriptors before it is reaped.
CONTROLLER_GONE_SECONDS = 10.0
#: How long the workers of a killed distributed run are given to finish.
#: execnet sends each of them SIGINT five seconds after the controller is
#: gone and ``os._exit``s them ten seconds after that; a worker still alive
#: past this is not a death and is not reported as one.
WORKERS_GONE_SECONDS = 20.0
POLL_SECONDS = 0.25


# -- the callable, in transit -----------------------------------------------


def describe_callable(target: Any) -> dict[str, str]:
    """What the controller hands the sidecar: a pickle, or a dotted path.

    Raises whatever ``pickle`` raises for a callable that cannot travel, so
    the controller can say so at session start rather than the reporter
    finding out after the run is dead.
    """
    if isinstance(target, str):
        return {"path": target}
    return {"pickle": base64.b64encode(pickle.dumps(target)).decode("ascii")}


def resolve(spec: dict[str, Any]) -> Callable[[Any], Any]:
    """The callable back, in the reporter."""
    target: Any
    if "pickle" in spec:
        target = pickle.loads(base64.b64decode(spec["pickle"]))
    else:
        module_name, _, attribute = str(spec["path"]).partition(":")
        if not attribute:
            module_name, _, attribute = module_name.rpartition(".")
        target = importlib.import_module(module_name)
        for part in attribute.split("."):
            target = getattr(target, part)
    if not callable(target):
        raise TypeError(f"{target!r} is not callable")
    return target


# -- the report -------------------------------------------------------------


def report(payload: dict[str, Any]) -> list[Any]:
    """Build the dead run's incidents and hand each to the callable.

    Returns what was reported. The callable is called once per incident,
    the way the hook is; an exception is logged and leaves remaining
    deliveries for a later retry. Successful callbacks are checkpointed.
    """
    _restore_import_path(payload)
    target = resolve(payload["callable"])

    # Imported only now, on the restored path, so a dev install that lives
    # off PYTHONPATH resolves the same way it did in the controller.
    from .. import probes
    from ..analysis.attribution import Attributor
    from ..capture.state import read_state
    from . import leftovers
    from .enrich import enrich

    directory = Path(payload["directory"])
    session = str(payload.get("session") or directory.name)
    _wait_until_gone(int(payload["controller_pid"]), CONTROLLER_GONE_SECONDS, probes.is_running)
    worker_pids = [
        int(record["pid"])
        for record in (read_state(state, None) for state in directory.glob("*.state"))
        if isinstance(record.get("pid"), int)
    ]
    _wait_until_all_gone(worker_pids, WORKERS_GONE_SECONDS, probes.is_running)

    with leftovers.claim(directory) as acquired:
        if not acquired:
            return []
        marker = leftovers.marker(directory)
        if marker is None or marker.get(leftovers.FINISHED_KEY) or marker.get(leftovers.REPORTED_KEY):
            return []
        found = leftovers.deaths_of(directory, elevate=bool(payload.get("elevate")))
        if not found:
            return []
        # Do not close a run while a surviving worker can still leave evidence.
        if any(isinstance(incident.worker_pid, int) and probes.is_running(incident.worker_pid)
               for incident in found):
            return []
        attributor = Attributor(tuple(payload.get("packages") or ()))
        def send(incident: Any) -> None:
            enrich(incident, attributor, payload.get("product_version"), session)
            target(incident)
        try:
            return leftovers.deliver(directory, found, send)
        except Exception:
            traceback.print_exc()
            return []  # checkpointed successes survive; failures remain retryable


def _restore_import_path(payload: dict[str, Any]) -> None:
    """The controller's ``sys.path``, so its conftest and its dev installs
    import here the way they did there."""
    entries = list(payload.get("sys_path") or [])
    rootdir = payload.get("rootdir")
    if rootdir:
        entries.insert(0, str(rootdir))
    for entry in reversed(entries):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def _wait_until_gone(pid: int, timeout: float, is_running: Callable[[int], bool]) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and is_running(pid):
        time.sleep(POLL_SECONDS)


def _wait_until_all_gone(
    pids: list[int], timeout: float, is_running: Callable[[int], bool]
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(is_running(pid) for pid in pids):
        time.sleep(POLL_SECONDS)


def main(stream: Optional[Any] = None) -> int:
    """Entry point for the sidecar's child: the payload on stdin, the log on
    stderr, nothing on stdout."""
    try:
        payload = json.load(stream or sys.stdin)
        reported = report(payload)
    except Exception:  # noqa: BLE001 - the log is the only reader
        traceback.print_exc()
        return 1
    print(f"reported {len(reported)} incident(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
