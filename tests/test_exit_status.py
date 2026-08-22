"""Decoding an exit status is a pure function of an integer, which is the only
reason the Windows codes below can be tested on a Linux runner at all."""

from __future__ import annotations

import signal

import pytest

from pytest_failure_instrumentation.analysis import exit_status


def test_none_is_unknown():
    assert exit_status.describe(None) == "unknown"


def test_plain_exit_code():
    assert exit_status.describe(3) == "process exited with code 3"


@pytest.mark.skipif(not hasattr(signal, "SIGSEGV"), reason="POSIX signals")
def test_fatal_signal_names_the_fault():
    described = exit_status.describe(-signal.SIGSEGV)
    assert "SIGSEGV" in described
    assert "segmentation fault" in described


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="POSIX signals")
def test_deliberate_signal_is_described_as_a_request():
    described = exit_status.describe(-signal.SIGTERM)
    assert "SIGTERM" in described
    assert "shutdown request" in described


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="no SIGKILL here")
def test_sigkill_does_not_claim_oom():
    described = exit_status.describe(-signal.SIGKILL)
    assert "SIGKILL" in described
    # The whole point: -9 alone never licenses an OOM verdict.
    assert "or external kill" in described


@pytest.mark.parametrize(
    "status, expected_verdict, fragment",
    [
        (3221225477, "NATIVE_CRASH", "access violation"),
        (3221225725, "NATIVE_CRASH", "stack overflow"),
        (3221226505, "NATIVE_CRASH", "stack buffer overrun"),
        (3221225786, "INTERRUPTED", "control-C"),
    ],
)
def test_windows_ntstatus(status, expected_verdict, fragment):
    verdict, description = exit_status.WINDOWS_STATUS[status]
    assert verdict == expected_verdict
    assert fragment in description
    assert fragment in exit_status.describe(status)


def test_wrapper_convention_is_reported_as_a_convention():
    # 128 + SIGTERM, as a shell or container runtime reports it.
    described = exit_status.describe(143)
    assert "143" in described
    assert "conventionally" in described
