"""Harness for the integration tests.

Each one runs a real pytest in a subprocess - under xdist where the kind needs
it - and reads back what the plugin raised. The inner run writes incidents as
JSON so the outer run can parse them into models, which also exercises the
round-trip on every scenario rather than in one test of its own.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pytest_failure_instrumentation.incidents.base import Incident
from pytest_failure_instrumentation.incidents.registry import parse

pytest_plugins = ["pytester"]

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
        self._result: Any = None
        #: The arguments and deadline of a run that never came back, or None.
        #: Kept so that :attr:`result` can say that rather than hand out the
        #: nothing it has.
        self._timed_out: Any = None

    @property
    def result(self) -> Any:
        """What the inner run itself reported, for the cases where the point is
        that the failure stayed inside the process.

        A run that outlives its timeout reports nothing, and every test that
        reads this then died on ``'NoneType' object has no attribute 'stderr'``
        - which names neither the timeout nor the run that hit it. A macOS
        cell spent a CI cycle on that. The cases where a wedged worker *is*
        the point read incidents off disk and never touch this, so arriving
        here with nothing means a run that was meant to finish did not, and
        that is worth saying in those words.
        """
        if self._result is None and self._timed_out is not None:
            arguments, timeout = self._timed_out
            pytest.fail(
                f"the inner pytest run did not finish within {timeout}s and so "
                f"reported nothing to assert on.\n  pytest "
                f"{' '.join(str(argument) for argument in arguments)}"
                f"{self._what_it_managed_to_say()}"
            )
        return self._result

    def _what_it_managed_to_say(self) -> str:
        """The tail of a timed-out run's own output, if pytester left any.

        Without it a timeout is only a number: the interesting question is
        always whether the run hung or was merely slow, and the last lines it
        printed are what separate those.
        """
        parts = []
        for name in ("stdout", "stderr"):
            path = self.pytester.path / name
            try:
                tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
            except OSError:
                continue
            if tail:
                parts.append(f"\n  --- its {name} (last {len(tail)} lines) ---\n    " + "\n    ".join(tail))
        return "".join(parts)

    def run(self, *arguments: str, timeout: float = 300.0) -> list[Incident]:
        try:
            self._result = self.pytester.runpytest_subprocess(*arguments, timeout=timeout)
        except self.pytester.TimeoutExpired:  # type: ignore[attr-defined]
            # A wedged worker is the point of some of these; the incident is
            # already on disk by then, and those tests read incidents rather
            # than result. Recorded so that a test which does read result says
            # the run timed out instead of dying on the None left behind.
            self._timed_out = (arguments, timeout)
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


# The whole set: xdist's remote.py exports exactly these three as a worker
# starts, and nothing unsets them for a process that worker then spawns.
XDIST_WORKER_ENV = (
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_XDIST_TESTRUNUID",
)


@pytest.fixture(autouse=True)
def _hide_the_outer_runs_xdist_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this suite's own -n out of the pytest runs it starts.

    pytester scrubs PYTEST_ADDOPTS and TOX_ENV_DIR before a subprocess run, and
    nothing xdist sets - so under -n every inner run inherits the outer
    worker's identity and any conftest that reads it believes it is a worker
    when it is a controller. That cost three deterministic failures under -n 4:
    the inner conftest in test_worker_death killed the inner controller, and
    the "main" branch in test_internal_error never fired at all.

    Autouse rather than a dependency of the runner fixture, so an inner run
    added later is covered without anyone having to remember this.
    """
    for name in XDIST_WORKER_ENV:
        monkeypatch.delenv(name, raising=False)


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
