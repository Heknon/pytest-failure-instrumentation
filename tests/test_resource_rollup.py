"""Proportional memory accuracy and isolated Linux probe failures."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pytest_failure_instrumentation.probes.resource_metrics import PlatformMetrics, smaps_rollup

HEADER = '00400000-00800000 ---p 00000000 00:00 0 [rollup]\n'


def test_rollup_units_private_sum_and_optional_fields(tmp_path):
    path = tmp_path / 'smaps_rollup'
    path.write_text(HEADER + 'Rss: 100 kB\nPss: 60 kB\nPrivate_Clean: 8 kB\n'
                    'Private_Dirty: 12 kB\nPss_Anon: 30 kB\nSwapPss: 0 kB\n'
                    'Private_Hugetlb: 2048 kB\n')
    values, missing = smaps_rollup(path)
    assert values['pss_bytes'] == 60 * 1024
    assert values['uss_bytes'] == 20 * 1024
    assert values['pss_anonymous_bytes'] == 30 * 1024
    assert values['swap_pss_bytes'] == 0
    assert missing == {'pss_file_bytes': 'unsupported', 'pss_shared_bytes': 'unsupported'}


@pytest.mark.parametrize('raw', ['bad kB', '-1 kB', '12 MB', '12', ''])
def test_bad_field_preserves_other_measurements_without_inventing_uss(tmp_path, raw):
    path = tmp_path / 'smaps_rollup'
    path.write_text(HEADER + f'Pss: 60 kB\nPrivate_Clean: {raw}\nPrivate_Dirty: 12 kB\n')
    values, missing = smaps_rollup(path)
    assert values['pss_bytes'] == 61440
    assert 'uss_bytes' not in values
    assert missing['private_clean_bytes'] == 'invalid_value'
    assert missing['uss_bytes'] == 'incomplete_private_fields'


@pytest.mark.parametrize('error,why', [(PermissionError(), 'permission_denied'),
                                      (FileNotFoundError(), 'not_found'),
                                      (ProcessLookupError(), 'process_gone')])
def test_rollup_failure_is_additive_and_keeps_rss(monkeypatch, error, why):
    probe = PlatformMetrics()
    probe.system = 'Linux'
    process = MagicMock()
    process.pid = 123
    process.cpu_times.return_value = (1, 2)
    process.memory_info.return_value = SimpleNamespace(rss=4096)
    reads = []

    def read(path, *args, **kwargs):
        reads.append(path.name)
        raise error

    monkeypatch.setattr(Path, 'read_text', read)
    try:
        values, missing = probe.process(process)
    finally:
        probe.close()
    assert values['rss_bytes'] == 4096
    assert values['cpu_total_seconds'] == 3
    assert 'pss_bytes' not in values
    assert missing['pss_bytes'] == why
    assert reads.count('smaps_rollup') == 1
    assert 'smaps' not in reads


def test_non_linux_does_not_read_rollup(monkeypatch):
    probe = PlatformMetrics()
    probe.system = 'Windows'
    process = MagicMock()
    process.cpu_times.return_value = (1, 2)
    monkeypatch.setattr(Path, 'read_text', lambda *a, **kw: pytest.fail('procfs read'))
    try:
        values, missing = probe.process(process)
    finally:
        probe.close()
    assert 'pss_bytes' not in values
    assert 'pss_bytes' not in missing
