"""Whether a worker can actually be read where Linux restricts who may read.

Every other test of the live view runs wherever it happens to run. This one is
about the *policy* a machine enforces, so it says which policy it observed and
skips rather than passing quietly when the interesting one is absent.

The mechanism under test: py-spy is spawned by the controller and a worker is
spawned by the controller, so the two are siblings - and Yama at ptrace_scope=1
requires the tracer to be an ancestor of its target. A sibling is not one. The
worker therefore nominates its parent, and Yama permits that pid's descendants,
which is what py-spy is.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap
import time

import pytest

from pytest_failure_instrumentation.probes import pyspy, tracing

from .conftest import INNER_CONFTEST, needs_xdist

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

needs_pyspy = pytest.mark.skipif(
    not pyspy.available(), reason="py-spy is not installed in this environment"
)
needs_restriction = pytest.mark.skipif(
    tracing.ptrace_scope() != 1,
    reason=f"this machine enforces ptrace_scope={tracing.ptrace_scope()}, "
    "and only 1 restricts a sibling read",
)

VICTIM = """
import json, os, sys, time

sys.path.insert(0, {src!r})
from pytest_failure_instrumentation.probes import tracing

# The shipping function, not a copy of it: a test that reimplements the call
# proves its own reimplementation works.
granted = tracing.permit_tracing({policy!r})
print(json.dumps({{
    "granted": granted,
    "scope": tracing.ptrace_scope(),
    "pid": os.getpid(),
    "ppid": os.getppid(),
}}), flush=True)


def parked():
    time.sleep(60)


parked()
"""


def _read_a_sibling(policy):
    """Spawn a victim under ``policy``, ask py-spy for its stack.

    Returns ``(threads, error, declared)`` where ``declared`` is what the
    victim reported about its own declaration - which is the difference
    between "the kernel ignored a good declaration" and "the declaration never
    happened", and guessing between those costs a CI cycle each time.
    """
    source = textwrap.dedent(
        VICTIM.format(src=str(SRC), policy=policy)
    )
    victim = subprocess.Popen(
        [sys.executable, "-c", source], stdout=subprocess.PIPE, text=True
    )
    declared = {}
    try:
        line = victim.stdout.readline() if victim.stdout else ""
        try:
            declared = json.loads(line)
        except ValueError:
            declared = {"unparsed": line.strip()}

        deadline = time.monotonic() + 20
        last_error = None
        while time.monotonic() < deadline:
            threads, error = pyspy.dump(victim.pid, timeout=10.0)
            if threads and any(
                frame.get("function") == "parked"
                for thread in threads
                for frame in thread.get("frames", [])
            ):
                return threads, None, declared
            last_error = error
            if error and "permitted" in error.lower():
                return None, error, declared
            time.sleep(0.2)
        return None, last_error or "the victim never parked", declared
    finally:
        victim.kill()
        victim.wait(timeout=10)


def test_this_machines_ptrace_policy_is_reported_rather_than_assumed():
    """A number worth having in the log of every run: it decides whether the
    live view can read anything at all, and it is not this process's to set."""
    scope = tracing.ptrace_scope()
    assert scope is None or scope in (0, 1, 2, 3)


@needs_pyspy
@needs_restriction
def test_a_worker_that_grants_the_exception_can_be_read(tmp_path):
    """The fix, under the policy it exists for."""
    threads, error, declared = _read_a_sibling("parent")
    assert declared.get("granted") is True, (
        f"the declaration itself failed, so nothing downstream is being "
        f"tested: {declared}"
    )
    assert threads, f"a granting sibling could not be read: {error} | {declared}"


@needs_pyspy
@needs_restriction
def test_a_worker_that_grants_nothing_is_refused(tmp_path):
    """The other half, and the one that proves the first is not passing for
    free. Without the exception this read *must* fail at ptrace_scope=1 - if it
    succeeds, the machine is not enforcing what the skip condition claims and
    the test above proves nothing."""
    threads, error, declared = _read_a_sibling("off")
    assert threads is None, (
        "a sibling with no exception was readable at ptrace_scope=1, so this "
        "machine is not restricting what it reports it restricts"
    )
    assert error and "permitted" in error.lower(), error
    assert declared.get("granted") is False, declared


@pytest.mark.parametrize("policy", tracing.POLICIES)
def test_every_policy_answers_without_raising(policy):
    """This runs on a worker's startup path, so the one thing it must never do
    is raise - a missing exception costs a stack, an exception costs the run."""
    assert isinstance(tracing.permit_tracing(policy), bool)


def test_off_declares_nothing_even_where_the_kernel_would_accept_it():
    """The escape hatch has to actually be one: somebody who does not want
    their test processes advertising a tracer must be able to say so."""
    assert tracing.permit_tracing("off") is False


@needs_pyspy
@needs_restriction
def test_the_any_policy_is_what_a_shared_reader_needs():
    """Why "parent" is not simply always right.

    A named port is served by whichever session claimed it, so the reader is a
    descendant of *that* controller - and a worker that nominated its own
    controller has said nothing about this one. Only "any" covers a reader
    that is nobody's descendant, which is exactly the shared case.
    """
    threads, error, declared = _read_a_sibling("any")
    assert declared.get("granted") is True, (
        f"the ANY declaration itself failed: {declared}"
    )
    assert threads, f"a worker granting ANY could not be read: {error} | {declared}"


# -- what a real run declares, as against what it could -------------------


def _declarations(pytester):
    """Every worker's ``worker_start``, read from the run's own evidence.

    Read from the file rather than from a probe, because the point is what
    reached ``WorkerRecorder`` in a different process: a test that called
    ``permit_tracing`` itself would prove only that the function works, which
    is what every other test in this file already covers.
    """
    # One directory per run, named for the session - see "Two runs at once".
    evidence = pytester.path / ".pytest-failures"
    started = []
    for path in sorted(evidence.glob("*/*.events")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "worker_start":
                started.append(event)
    return started


@needs_xdist
def test_a_run_with_nothing_reading_stacks_declares_no_tracer(distributed):
    """The install-and-do-nothing run, which is nearly all of them.

    Every worker used to declare a tracer here whatever the run was for. On
    Ubuntu and Debian - ptrace_scope=1 out of the box - that made ``pip
    install --upgrade`` plus ``pytest -n8`` a widening of who may read a test
    process, for a live view nobody had switched on.
    """
    distributed.pytester.makeini(
        """
        [pytest]
        failure_packages = victim
        """
    )
    distributed.pytester.makeconftest(INNER_CONFTEST)
    distributed.pytester.makepyfile(test_quick="def test_one():\n    assert True\n")

    distributed.run("-n", "1", "test_quick.py", timeout=180)

    started = _declarations(distributed.pytester)
    assert started, "the worker recorded no startup at all"
    for event in started:
        assert event["tracer_policy"] == "off", event
        # "off" declares nothing on every platform, so this is the same
        # assertion on a machine with no Yama as on one that enforces it.
        assert event["traceable_by_parent"] is False, event


@needs_xdist
def test_the_configured_policy_is_what_the_worker_declares(distributed):
    """The ini reaching the process that acts on it.

    Nothing else exercises that end to end: "any" and "off" were only ever
    reached by calling ``permit_tracing`` directly, so the wiring between the
    setting and the declaration - which crosses a process boundary and drops
    the settings that decide it on the way - was carried by nobody.

    The sampler is switched on because that is one of the two things whose
    presence makes a declaration worth making; without it the right answer is
    "off" whatever the policy says, which is the test above.
    """
    distributed.pytester.makeini(
        """
        [pytest]
        failure_packages = victim
        failure_tracer = any
        failure_sample_seconds = 1
        """
    )
    distributed.pytester.makeconftest(INNER_CONFTEST)
    distributed.pytester.makepyfile(test_quick="def test_one():\n    assert True\n")

    distributed.run("-n", "1", "test_quick.py", timeout=180)

    started = _declarations(distributed.pytester)
    assert started, "the worker recorded no startup at all"
    for event in started:
        assert event["tracer_policy"] == "any", event
