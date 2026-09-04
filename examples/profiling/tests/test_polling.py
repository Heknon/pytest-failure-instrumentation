"""Scenario 3: a background thread from a session fixture. Expected:
cpu_hotspot BACKGROUND_THREAD blamed on poller.py in Poller._run, on thread
'status-poller' - the CPU is paid whatever test is running, these included."""

import time


def test_with_the_poller_running(status_poller):
    time.sleep(3.0)
    assert status_poller.polls > 0


def test_another_with_the_poller_running(status_poller):
    time.sleep(3.0)
    assert status_poller.polls > 0
