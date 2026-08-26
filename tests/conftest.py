"""Harness for the integration tests.

Each one runs a real pytest in a subprocess - under xdist where the kind needs
it - and reads back what the plugin raised. The inner run writes incidents as
JSON so the outer run can parse them into models, which also exercises the
round-trip on every scenario rather than in one test of its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pytest_failure_instrumentation.incidents.base import Incident
from pytest_failure_instrumentation.incidents.registry import parse
from pytest_failure_instrumentation.probes import is_running
from pytest_failure_instrumentation.probes.platform_flags import optional_psutil

pytest_plugins = ["pytester"]

#: A pid nothing is using, for asking whether this environment can recognise a
#: process that is not there.
DEAD_PID = 999999

#: Whether a dead process can be told from a live one *here*. It needs procfs
#: or psutil, and an environment with neither - psutil is a dependency, so that
#: means one where it would not install - has ``is_running`` erring towards
#: "still there" by design, because a wrong "it died" deletes evidence and
#: reports a working worker as gone. Correct, and it means the tests that turn
#: on recognising a dead process are asserting a capability that is absent
#: rather than a behaviour that is wrong.
needs_process_liveness = pytest.mark.skipif(
    is_running(DEAD_PID),
    reason="no way to tell a dead process here: no procfs and no psutil",
)

#: Recognising a *zombie* is a strictly harder question than recognising a pid
#: that was never there. POSIX answers the second with signal 0 whatever else
#: is installed; the first needs procfs or psutil, because the kernel keeps
#: accepting signals for a process that has died and not yet been reaped. So
#: macOS without psutil can tell a nonexistent pid from a live one and cannot
#: tell a zombie from a live one - which is a real, documented gap and not a
#: wrong answer: the heartbeat carries that finding instead, a beat later.
needs_zombie_detection = pytest.mark.skipif(
    optional_psutil() is None and not Path("/proc/self/stat").exists(),
    reason="zombies need procfs or psutil; the heartbeat carries it here instead",
)

INCIDENT_FILE = "incidents.jsonl"

INNER_CONFTEST = f"""
import json


def pytest_failure_incident(incident):
    with open({INCIDENT_FILE!r}, "a") as handle:
        handle.write(incident.model_dump_json() + "\\n")
"""

VICTIM_MODULE = '''
import ctypes
import os
import sys

ACCESS_VIOLATION = 0xC0000005


def native_call(pointer):
    """A fault in native code, the way a C extension produces one.

    On POSIX this is a SIGSEGV and the process is gone before anything can
    react. Windows cannot be made to do that through ctypes: every foreign
    function call is wrapped in structured exception handling, so an access
    violation comes back as an OSError and the worker survives it.

    abort() is a real death there instead, and faulthandler still writes a
    dump for it - which is what the attribution has to work from. It exits
    with 3 rather than an NTSTATUS, so the crash is told from a plain
    os._exit(3) by the presence of that dump and nothing else.
    """
    if sys.platform != "win32":
        return ctypes.string_at(pointer)
    os.abort()


def exit_with_ntstatus(status=ACCESS_VIOLATION):
    """Die with the exit code Windows reports for a real fault.

    argtypes and restype are set because GetCurrentProcess returns a HANDLE:
    ctypes defaults to a 32-bit int, which truncates the pseudo-handle and
    makes TerminateProcess fail silently.
    """
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    kernel32.TerminateProcess(kernel32.GetCurrentProcess(), status)


def hard_exit(code):
    os._exit(code)


def break_pytest():
    raise RuntimeError("victim broke pytest's machinery")
'''


class Runner:
    def __init__(self, pytester: pytest.Pytester) -> None:
        self.pytester = pytester
        #: What the inner run itself reported, for the cases where the point is
        #: that the failure stayed inside the process.
        self.result: Any = None

    def run(self, *arguments: str, timeout: float = 300.0) -> list[Incident]:
        try:
            self.result = self.pytester.runpytest_subprocess(*arguments, timeout=timeout)
        except self.pytester.TimeoutExpired:  # type: ignore[attr-defined]
            # A wedged worker is the point of some of these; the incident is
            # already on disk by then.
            pass
        return self.incidents()

    def incidents(self) -> list[Incident]:
        path = self.pytester.path / INCIDENT_FILE
        if not path.exists():
            return []
        found = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            incident = parse(raw)
            # Every scenario doubles as a round-trip test: what a database
            # would store has to come back as the model it was written from.
            assert incident.model_dump() == raw, incident.kind
            found.append(incident)
        return found

    @staticmethod
    def of_kind(incidents: list[Incident], kind: str) -> list[Incident]:
        return [incident for incident in incidents if incident.kind == kind]

    @staticmethod
    def only(incidents: list[Incident], kind: str) -> Any:
        matching = Runner.of_kind(incidents, kind)
        assert len(matching) == 1, f"expected one {kind}, got {[i.kind for i in incidents]}"
        return matching[0]


@pytest.fixture
def runner(pytester: pytest.Pytester) -> Runner:
    pytester.makeconftest(INNER_CONFTEST)
    pytester.makeini(
        """
        [pytest]
        failure_packages = victim
        failure_product_version = 1.2.3
        """
    )
    (pytester.path / "victim.py").write_text(VICTIM_MODULE, encoding="utf-8")
    return Runner(pytester)


@pytest.fixture
def distributed(runner: Runner) -> Runner:
    if not _has_xdist():
        pytest.skip("pytest-xdist is not installed")
    return runner


def _has_xdist() -> bool:
    try:
        import xdist  # noqa: F401
    except ImportError:
        return False
    return True


needs_xdist = pytest.mark.skipif(not _has_xdist(), reason="needs pytest-xdist")
