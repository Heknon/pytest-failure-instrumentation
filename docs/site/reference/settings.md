---
title: "Settings"
description: "Every ini setting, command-line option and environment variable pytest-failure-instrumentation reads, with its default."
pageType: reference
owner: "morphine"
---

Every setting pytest-failure-instrumentation reads. Settings go in the
`[pytest]` section of `pytest.ini` (or its equivalent) and are read only once a
run has switched the plugin on.

## Command-line options

| Option | Default | Purpose |
| --- | --- | --- |
| `--failure-instrumentation` | off | Switch the plugin on for this run |
| `--failure-profile` | off | Profile this run; switches the plugin on |
| `--failure-profile-allocations` | off | Profile with allocation tracing as well; implies `--failure-profile` |
| `--callstack-port PORT` | drawn | Serve live stacks on this port, shared with any other session on the host that names it |
| `--callstack-host HOST` | `127.0.0.1` | Interface the live stack server binds; `0.0.0.0` to reach it from outside a container |
| `--callstack-token SECRET` | — | Bearer token for the live stack endpoints; prefer `PYTEST_CALLSTACK_TOKEN` |

## Environment variables

| Variable | Purpose |
| --- | --- |
| `PYTEST_CALLSTACK_TOKEN` | Bearer token for the live stack server. Required to bind anything but loopback. There is no ini setting for it. |
| `PYTEST_RUN_ID` | Names the run's evidence directory: 1–128 letters, digits, `.`, `-` and `_`. |

## Ini settings

| Setting | Default | Purpose |
|---|---|---|
| `failure_packages` | — | Your top-level packages, for attribution |
| `failure_directory` | `.pytest-failures` | Where evidence is written; each run gets a subdirectory under it |
| `failure_product_version` | — | Version recorded on every incident, for telling which build a failure came from |
| `failure_watchdog` | `true` | Memory and liveness sampling |
| `failure_heartbeat_interval` | `5.0` | Seconds between liveness beats (floor 1.0) |
| `failure_tracemalloc_depth` | `0` | 1 names the allocating line for OOM attribution |
| `failure_object_census` | `false` | Count live objects at a high-water mark |
| `failure_high_water_mb` | auto | Memory mark for a snapshot; defaults to a share of the discovered limit |
| `failure_memory_limit_mb` | `0` | Soft cap (POSIX) turning an OOM kill into a `MemoryError` |
| `failure_slow_test_seconds` | `20` | How often a running test refreshes its stack (setup through teardown; needs `failure_watchdog`) |
| `failure_stall_seconds` | `300` | Silence before a stall is assessed |
| `failure_stack_probe` | `true` | Ask a diagnosed stalled worker for a fresh stack (POSIX) |
| `failure_crash_stack` | `false` | Keep the fatal stack of a run that has *no workers*, instead of leaving it on the stderr pytest points it at. A worker keeps its own either way |
| `failure_kill_trace` | `true` | Observe kills using Linux tracefs or Windows ETW where permitted, while preserving signal masks and handlers. |
| `failure_elevate` | `false` | Allow `sudo -n` for the witnesses that need root: `dmesg` where `/dev/kmsg` and the journal are closed, and the tracepoint above |
| `failure_capture_output` | `false` | Keep the last few KB of each worker's captured stderr, so a native crash message survives the kill that swallows it. |
| `failure_on_run_death` | — | A dotted path `package.module:attribute` to a callable the sidecar calls with each incident of a run whose controller was killed, so a killed run is reported at once. From Python, `install(config, on_run_death=functools.partial(...))`. |
| `failure_tracer` | `parent` | Who may read a worker on Linux under Yama: `parent`, `any`, `off`. Declared only when the stack server is on — a run with no reader declares nothing whatever this says |
| `failure_resources_seconds` | `0` | Live resource sample interval; off by default, minimum 1 second when enabled |
| `failure_resources_max_mb` | `256` | Numeric live-history disk budget in MiB, clamped to 8–1024 |
| `failure_resources_roots` | empty | Explicit directory inventory roots, at most eight |
| `failure_resources_scan_seconds` | `60` | Delay after each directory scan, minimum 10 seconds |
| `failure_resources_max_files` | `50000` | Entry budget per directory scan, clamped to 100–100000 |
| `failure_sample_seconds` | `0` | Push a worker sample this often while the run is going. 0 is off |
| `failure_stack_server` | `false` | Serve live stacks over HTTP |
| `failure_stack_server_port` | `0` | 0 draws a free port and writes it down; any other is claimed and shared (`--callstack-port`) |
| `failure_stack_server_host` | `127.0.0.1` | What it binds; `0.0.0.0` for a container (`--callstack-host`) |
| `failure_stack_server_locals` | `true` | Whether `/stack?locals` answers with each frame's variables |
| `failure_profile` | `false` | Sample every thread's stack and CPU for the whole run and raise what crosses the thresholds below (`--failure-profile` for one run) |
| `failure_profile_interval` | `0.02` | Seconds between profile samples (floor 0.002) |
| `failure_profile_cpu_share` | `5` | Percent of the run's CPU one function must hold to be raised |
| `failure_profile_cpu_floor_seconds` | `0.5` | Seconds of CPU one function must have used before its share counts, so that a short run does not raise the first thing it sampled |
| `failure_profile_retained_mb` | `100` | Megabytes a test may keep, or climb by, before it is raised |
| `failure_profile_peak_mb` | `0` | Resident megabytes no test may reach, whatever it started from; 0 is off |
| `failure_profile_growth_mb` | `5` | Megabytes a run's tests may add between them, in use, before the run is raised as growing. Low, because what it is really the bar for is a cost that **recurs**, and a recurrence is paid again for every test the suite ever gains. `failure_profile_retained_mb` is what one *test* may keep and is a separate question |
| `failure_profile_step_mb` | `20` | Megabytes one test must have added on its own for memory that did **not** recur to be worth raising. A cost paid once is bounded — a module a test imported — so it is judged on its size, where a recurrence is judged on the fact that it repeats |
| `failure_profile_growth_per_test_mb` | `0` | Megabytes the *typical* test must have left behind as well, for a run that only wants to hear about a leak that repeats. 0 asks nothing of the shape |
| `failure_profile_growth_tests` | `4` | Tests that must each leave something behind before the drift between them is raised as steady growth — per worker, and again over the workers that did not reach the rule alone |
| `failure_profile_imbalance_ratio` | `2` | Times the median sibling's peak a worker must hold to be raised as imbalanced |
| `failure_profile_allocations` | `false` | Trace allocations with tracemalloc as well, naming the lines that hold the memory and writing memory flame graphs; tens of times slower on allocation-heavy pure-Python code, so for a rerun of the tests an untraced run named (`--failure-profile-allocations` for one run, which implies `--failure-profile`) |
| `failure_profile_allocation_depth` | `12` | Frames kept per allocation when tracing |
| `failure_profile_burst_cores` | `0.7` | Cores' worth of CPU a tenth-of-a-second window must hold to be part of a burst |
| `failure_profile_burst_seconds` | `2` | Seconds a burst must last to be raised as one test's own; the same function bursting in five tests is raised whatever their length |
