# Profiling examples

A suite whose every module is one thing the profiler is meant to flag, with
the finding each one expects in its docstring. `demo_product` stands in for
the product under test and is named in `failure_packages`, so the findings
carry `owner=product` the way they would for yours.

```console
cd examples/profiling
pytest                        # one process
pytest -n 2 --dist loadfile   # two workers: adds the imbalance finding
pytest --failure-profile-allocations tests/test_drift.py tests/test_memory.py
                              # the rerun a growth finding asks for: the
                              # lines holding the memory, and memory flame
                              # graphs beside the CPU ones
```

The findings print in the terminal summary and land in `incidents.jsonl`
through the `pytest_failure_incident` hook in `tests/conftest.py`. A
speedscope flame graph for every test a finding names is written under
`.pytest-failures/run-*/profiles/`.

| Module | Finding |
|---|---|
| `test_screenshots.py` | `cpu_hotspot` `PYTHON_CODE`: a per-pixel loop in the product |
| `test_reports.py` | `cpu_hotspot` `LIBRARY_CALL`: the cost is under the json encoder |
| `test_polling.py` | `cpu_hotspot` `BACKGROUND_THREAD`: a session fixture's poller |
| `test_allocation.py` | `cpu_hotspot` `GC_PRESSURE`: millions of small objects |
| `test_memory.py` | `memory_profile` `RETAINED_AFTER_TEST` (body and fixture), `TRANSIENT_PEAK`, and under `-n 2` `WORKER_IMBALANCE` |
| `test_loading.py` | `memory_profile` `PEAK_OVER_CEILING`: a loader that does not stream |
| `test_sessions.py` | `cpu_burst` `RECURRING_BURST`: an I/O suite whose fixture bursts on every test |
| `test_index.py` | `cpu_burst` `LONG_BURST`: one CPU step inside a test that otherwise waits |
| `test_drift.py` | `memory_profile` `STEADY_GROWTH`, together with `test_memory.py`'s small leaks: the worker drifting up a few megabytes a test |
| `test_arenas.py` | `memory_profile` `ALLOCATOR_RETENTION`: a thread pool whose arenas keep what they freed — the `MALLOC_ARENA_MAX` finding |
| `test_healthy.py` | nothing — the control |
