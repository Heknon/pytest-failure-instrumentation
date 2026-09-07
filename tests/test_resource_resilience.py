"""Fault injection and concurrent live-resource contract checks."""
from __future__ import annotations

import errno
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pytest_failure_instrumentation.capture import resource_history as rh
from pytest_failure_instrumentation.incidents import leftovers
from pytest_failure_instrumentation.probes.resource_metrics import PlatformMetrics

from .test_resources import batch, current_process, history


def test_finished_run_is_inaccessible_even_if_cleanup_and_writes_fail(tmp_path, monkeypatch):
    store = history(tmp_path)
    store.append(batch())
    sentinel = tmp_path / 'gw0.events'
    sentinel.write_text('keep incident evidence')
    attempts = []

    def locked(*args, **kwargs):
        attempts.append(1)
        raise PermissionError('file held by another process')

    with monkeypatch.context() as patch:
        patch.setattr(rh.shutil, 'rmtree', locked)
        patch.setattr(rh, 'atomic_json', locked)
        assert not store.close()
        assert store.cleanup_error
        assert len(attempts) == 4
        with pytest.raises(FileNotFoundError):
            rh.read_history(tmp_path)
        with pytest.raises(FileNotFoundError):
            store.append(batch())
    assert store.close()  # retry after the lock is released
    assert not store.directory.exists()
    assert sentinel.read_text() == 'keep incident evidence'


def test_finished_embedded_run_retries_resource_cleanup_without_deleting_incidents(tmp_path, monkeypatch):
    run = tmp_path / 'run'
    run.mkdir()
    owner = current_process()
    monkeypatch.setattr(leftovers.probes, 'is_running', lambda pid: pid == owner.pid)
    (run / 'owner.json').write_text(json.dumps({'pid': owner.pid, 'finished_at': time.time()}))
    resources = run / rh.NAME
    resources.mkdir()
    (resources / 'remaining.sqlite').touch()
    (run / 'gw0.events').write_text('keep')
    leftovers.prune_finished_runs(tmp_path)
    assert not resources.exists()
    assert (run / 'gw0.events').read_text() == 'keep'


def test_live_lease_is_visible_across_processes_and_ends_before_owner_exit(tmp_path):
    store = history(tmp_path)
    command = [sys.executable, '-c',
               'from pathlib import Path; from pytest_failure_instrumentation.capture.resource_history '
               'import is_active; import sys; print(is_active(Path(sys.argv[1])))', str(store.directory)]
    try:
        assert subprocess.check_output(command, text=True, timeout=10).strip() == 'True'
        store.deactivate()
        assert subprocess.check_output(command, text=True, timeout=10).strip() == 'False'
    finally:
        store.close()


def test_reader_does_not_expose_unpublished_records(tmp_path, monkeypatch):
    store = history(tmp_path)
    try:
        store.append(batch(1))
        with monkeypatch.context() as patch:
            def unavailable():
                raise OSError(errno.ENOSPC, 'manifest could not be published')
            patch.setattr(store, '_publish', unavailable)
            with pytest.raises(OSError):
                store.append(batch(2))
        page = rh.read_history(tmp_path)
        assert page['latest_sequence'] == 1
        assert [b['sequence'] for b in page['batches']] == [1]
        store.append(batch(3))
        assert [b['sequence'] for b in rh.read_history(tmp_path)['batches']] == [1, 2, 3]
    finally:
        store.close()


def test_short_write_recovers_without_poisoning_following_batches(tmp_path, monkeypatch):
    store = history(tmp_path)
    store.append(batch(1))
    original = Path.open

    class Partial:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.handle.close()
        def __getattr__(self, name):
            return getattr(self.handle, name)
        def write(self, data):
            self.handle.write(data[:13])
            self.handle.flush()
            raise OSError(errno.ENOSPC, 'short disk write')

    def open_file(path, *args, **kwargs):
        handle = original(path, *args, **kwargs)
        return Partial(handle) if path.suffix == '.jsonl' and args == ('r+b',) else handle

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'open', open_file)
            with pytest.raises(OSError):
                store.append(batch(2))
        assert [b['sequence'] for b in rh.read_history(tmp_path)['batches']] == [1]
        store.append(batch(3))
        page = rh.read_history(tmp_path)
        assert [b['sequence'] for b in page['batches']] == [1, 2]
        assert page['batches'][-1]['observed_at'] == 3
        assert page['history_bytes'] == sum(p.stat().st_size for p in store.directory.glob('*.jsonl'))
    finally:
        store.close()


def test_read_rechecks_lifecycle_before_returning_data(tmp_path, monkeypatch):
    store = history(tmp_path)
    store.append(batch())
    original = rh.is_active
    calls = 0

    def active(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            store.deactivate()
        return original(path)

    try:
        monkeypatch.setattr(rh, 'is_active', active)
        with pytest.raises(FileNotFoundError):
            rh.read_history(tmp_path)
    finally:
        store.close()


def test_rates_preserve_counters_and_do_not_treat_capacity_as_traffic():
    probe = PlatformMetrics()
    try:
        first = {'read_time_ms': 100, 'read_total_count': 10, 'ram_total_bytes': 4096, 'swap_total_bytes': 2048}
        probe.rates('disk:x', first, 1)
        assert first['read_time_ms'] == 100
        assert first['read_time_per_second_ms'] is None
        assert 'ram_per_second_bytes' not in first
        assert 'swap_per_second_bytes' not in first
        second = {'read_time_ms': 160, 'read_total_count': 20}
        probe.rates('disk:x', second, 3)
        assert second['read_time_ms'] == 160
        assert second['read_time_per_second_ms'] == 30
        assert second['read_latency_ms'] == 6
    finally:
        probe.close()


def test_concurrent_readers_rotation_and_shutdown_are_bounded(tmp_path):
    store = history(tmp_path)
    store.segment_bytes = 256 * 1024  # many rotations inside a fixed 8 MiB budget
    stop = threading.Event()
    errors = []
    reads = [0, 0]

    def reader(index):
        after = 0
        try:
            while not stop.is_set():
                try:
                    page = rh.read_history(tmp_path, after=after, limit=7)
                except OSError:
                    continue  # contention/rotation is retryable; never a fake zero
                seqs = [b['sequence'] for b in page['batches']]
                assert seqs == sorted(set(seqs))
                assert all(after < s <= page['latest_sequence'] for s in seqs)
                assert page['next_after'] >= after
                after = page['next_after']
                reads[index] += 1
                stop.wait(0.002)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    try:
        for index in range(260):
            deadline = time.monotonic() + 5
            while True:
                try:
                    store.append(batch(index, 'x' * 65536))
                    break
                except PermissionError:
                    assert time.monotonic() < deadline, 'writer starved by readers'
                    time.sleep(0.002)
        stop.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        assert not errors, errors
        assert min(reads) > 0
        page = rh.read_history(tmp_path, latest=True)
        assert page['batches'][0]['observed_at'] == 259
        assert page['history_truncated']
        assert sum(p.stat().st_size for p in store.directory.glob('*.jsonl')) <= store.max_bytes
    finally:
        stop.set()
        for thread in threads:
            thread.join(5)
        store.close()
    assert not store.directory.exists()


def test_other_readers_cannot_make_a_finished_lease_appear_active(tmp_path):
    store = history(tmp_path)
    store.deactivate()
    try:
        with (store.directory / rh.LEASE).open('r+b') as reader:
            rh._lock(reader, shared=True)
            assert not rh.is_active(store.directory)
    finally:
        store.close()


def test_inventory_recovers_from_database_full_without_false_deletions(tmp_path):
    import sqlite3

    from pytest_failure_instrumentation.capture.file_resources import Scanner

    root = tmp_path / 'files'
    root.mkdir()
    (root / 'original').write_bytes(b'baseline')
    scanner = Scanner(root, tmp_path / 'inventory.sqlite', 3000, tmp_path / 'excluded', lambda value: None)
    try:
        baseline = scanner.scan()['baseline']
        scanner.db.execute('PRAGMA max_page_count=8')
        for index in range(1000):
            (root / (str(index) + 'x' * 80)).write_bytes(b'x')
        with pytest.raises(sqlite3.DatabaseError):
            scanner.scan()
        assert scanner.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        scanner.db.execute('PRAGMA max_page_count=8192')
        result = scanner.scan()
        assert result['status'] == 'complete'
        assert result['baseline'] == baseline
        assert result['new_remaining_count'] == 1000
        assert result['deleted_baseline_count'] == 0
    finally:
        scanner.close()


def test_helper_does_not_outlive_a_zombie_owner(tmp_path, monkeypatch):
    import psutil

    from pytest_failure_instrumentation.capture import file_resources

    class Zombie:
        def is_running(self):
            return True
        def create_time(self):
            return 123
        def status(self):
            return psutil.STATUS_ZOMBIE

    monkeypatch.setattr(psutil, 'Process', lambda pid: Zombie())
    file_resources.serve({'directory': str(tmp_path), 'pid': 1, 'created_at': 123, 'roots': []})
    assert not list(tmp_path.iterdir())


def test_corruption_inside_a_published_batch_is_not_silently_skipped(tmp_path):
    store = history(tmp_path)
    try:
        store.append(batch())
        path = next(store.directory.glob('*.jsonl'))
        with path.open('r+b') as stream:
            stream.write(b'INVALID')
        with pytest.raises(OSError, match='invalid published resource record'):
            rh.read_history(tmp_path)
    finally:
        store.close()


def test_cleanup_does_not_confuse_a_missing_child_with_a_removed_directory(tmp_path, monkeypatch):
    store = history(tmp_path)
    try:
        with monkeypatch.context() as patch:
            def missing_child(*args, **kwargs):
                raise FileNotFoundError('another cleanup removed this child')
            patch.setattr(rh.shutil, 'rmtree', missing_child)
            assert not store.close()
            assert store.cleanup_error
            assert not rh.is_active(store.directory)
    finally:
        assert store.close()
