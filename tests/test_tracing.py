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

import subprocess
import sys
import textwrap
import time

import pytest

from pytest_failure_instrumentation.probes import pyspy, tracing

needs_pyspy = pytest.mark.skipif(
    not pyspy.available(), reason="py-spy is not installed in this environment"
)
needs_restriction = pytest.mark.skipif(
    tracing.ptrace_scope() != 1,
    reason=f"this machine enforces ptrace_scope={tracing.ptrace_scope()}, "
    "and only 1 restricts a sibling read",
)

VICTIM = """
import ctypes, os, sys, time

GRANT = {grant!r}
if GRANT is not None:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl(0x59616D61, ctypes.c_ulong(GRANT if GRANT != "parent" else os.getppid()),
               0, 0, 0)


def parked():
    time.sleep(60)


parked()
"""


def _read_a_sibling(grant):
    """Spawn a victim, ask py-spy for its stack, return (threads, error).

    py-spy is spawned by *this* process and so is the victim, which makes them
    siblings - the same relationship a worker has to the reader in a real run.
    """
    source = textwrap.dedent(VICTIM.format(grant=grant))
    victim = subprocess.Popen([sys.executable, "-c", source])
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            threads, error = pyspy.dump(victim.pid, timeout=10.0)
            if threads and any(
                frame.get("function") == "parked"
                for thread in threads
                for frame in thread.get("frames", [])
            ):
                return threads, None
            if error and "permitted" in error.lower():
                return None, error
            time.sleep(0.2)
        return None, "the victim never parked where it could be read"
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
    threads, error = _read_a_sibling("parent")
    assert threads, f"a granting sibling could not be read: {error}"


@needs_pyspy
@needs_restriction
def test_a_worker_that_grants_nothing_is_refused(tmp_path):
    """The other half, and the one that proves the first is not passing for
    free. Without the exception this read *must* fail at ptrace_scope=1 - if it
    succeeds, the machine is not enforcing what the skip condition claims and
    the test above proves nothing."""
    threads, error = _read_a_sibling(None)
    assert threads is None, (
        "a sibling with no exception was readable at ptrace_scope=1, so this "
        "machine is not restricting what it reports it restricts"
    )
    assert error and "permitted" in error.lower(), error


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
    threads, error = _read_a_sibling(tracing.PTRACE_ANY)
    assert threads, f"a worker granting ANY could not be read: {error}"
