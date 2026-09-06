"""Real POSIX signals must not outrun the controller's death notification."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from pytest_failure_instrumentation.probes.signal_trace import SIDECAR

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")


def wait_for(path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(0.02)
    pytest.fail(f"No output at {path}")


def sidecar_source(result, grace=15.0):
    # Replace only the downstream callback runner. Exercise the actual sidecar
    # protocol, signal handler, EOF observation and subprocess launch.
    callback = f"import sys; open({str(result)!r}, 'w').write(sys.stdin.read())"
    source = SIDECAR.replace(
        'REPORTER = "from pytest_failure_instrumentation.incidents.reporter import main; main()"',
        f"REPORTER = {callback!r}",
    )
    return source.replace("SIGNAL_GRACE_SECONDS = 15.0", f"SIGNAL_GRACE_SECONDS = {grace!r}")


@pytest.mark.parametrize("order", ["watcher_first", "controller_first"])
def test_both_processes_receive_sigterm_and_report_controller_eof(tmp_path, order):
    result = tmp_path / "reported.json"
    output = tmp_path / "trace.jsonl"
    pidfile = tmp_path / "sidecar.pid"
    source = sidecar_source(result)
    # Only the disposable controller owns the pipe's write end. Killing it
    # produces real EOF, rather than having the test simulate a close.
    controller_code = f'''
import subprocess, sys, json, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", {source!r}, "test", "", {str(output)!r}, "", "watch"],
                         stdin=subprocess.PIPE, start_new_session=True)
child.stdin.write((json.dumps({{"reporter": {{"python": sys.executable, "sentinel": "controller"}}}})+"\\n").encode())
child.stdin.flush()
Path({str(pidfile)!r}).write_text(str(child.pid))
while True: time.sleep(.1)
'''
    controller = subprocess.Popen([sys.executable, "-c", controller_code])
    watcher = None
    try:
        wait_for(pidfile)
        watcher = int(pidfile.read_text())
        wait_for(output)
        if order == "watcher_first":
            os.kill(watcher, signal.SIGTERM)
            time.sleep(.2)  # Old handler exits before controller EOF here.
            controller.terminate()
        else:
            controller.terminate()
            os.kill(watcher, signal.SIGTERM)
        controller.wait(timeout=5)
        wait_for(result)
        assert json.loads(result.read_text())["sentinel"] == "controller"
    finally:
        if controller.poll() is None:
            controller.kill()
        controller.wait(timeout=5)
        if watcher is not None:
            try:
                os.kill(watcher, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("normal_stop", [False, True])
def test_watcher_signal_alone_does_not_report_and_deadline_is_bounded(tmp_path, normal_stop):
    result = tmp_path / "reported.json"
    output = tmp_path / "trace.jsonl"
    source = sidecar_source(result, grace=.6)
    child = subprocess.Popen([sys.executable, "-c", source, "test", "", str(output), "", "watch"],
                             stdin=subprocess.PIPE, start_new_session=True)
    try:
        child.stdin.write((json.dumps({"reporter": {"python": sys.executable}})+"\n").encode())
        child.stdin.flush()
        wait_for(output)
        began = time.monotonic()
        child.terminate()
        time.sleep(.2)
        assert child.poll() is None  # Signal alone does not immediately end it.
        if normal_stop:
            child.stdin.write(b'{"stop":true}\n')
            child.stdin.close()
        else:
            # Repeated signals cannot restart the deadline. Keep stdin open:
            # the controller is still alive and must never be reported dead.
            while child.poll() is None and time.monotonic() - began < 1.5:
                child.terminate()
                time.sleep(.05)
            assert child.poll() is not None, "repeated signals postponed the original deadline"
        # A repeated SIGTERM can arrive after CPython restores default
        # handlers during finalization. Grace duration and no false report
        # are the contract; that finalization race may set the exit status.
        assert child.wait(timeout=2) in ((0,) if normal_stop else (0, -signal.SIGTERM))
        elapsed = time.monotonic() - began
        assert elapsed < 1.5
        if not normal_stop:
            assert elapsed >= .55
        assert not result.exists()
    finally:
        if child.stdin and not child.stdin.closed:
            child.stdin.close()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize("signal_watcher", [False, True])
def test_forked_writer_cannot_hide_controller_death(tmp_path, signal_watcher):
    result = tmp_path / "reported.json"
    output = tmp_path / "trace.jsonl"
    pidfile = tmp_path / "pids.json"
    source = sidecar_source(result)
    code = f'''
import fcntl, json, os, subprocess, sys, time
from pathlib import Path
owner = open({str(output) + '.owner'!r}, "a+b")
fcntl.lockf(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
watcher = subprocess.Popen([sys.executable, "-c", {source!r}, "test", "", {str(output)!r}, "", "watch"],
                           stdin=subprocess.PIPE, start_new_session=True)
watcher.stdin.write((json.dumps({{"reporter": {{"python": sys.executable, "sentinel": "forked"}}, "ack": "ready"}})+"\\n").encode())
watcher.stdin.flush()
child = os.fork()
if child == 0:
    while True: time.sleep(.1)
Path({str(pidfile)!r}).write_text(json.dumps([watcher.pid, child]))
while True: time.sleep(.1)
'''
    controller = subprocess.Popen([sys.executable, "-c", code])
    children = []
    try:
        wait_for(pidfile)
        children = json.loads(pidfile.read_text())
        wait_for(tmp_path / "trace.jsonl.armed")
        if signal_watcher:
            os.kill(children[0], signal.SIGTERM)
            time.sleep(.2)
        controller.terminate()
        controller.wait(timeout=5)
        wait_for(result)
        assert json.loads(result.read_text())["sentinel"] == "forked"
        os.kill(children[1], 0)  # inherited writer is still alive
    finally:
        if controller.poll() is None:
            controller.kill()
        controller.wait(timeout=5)
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
