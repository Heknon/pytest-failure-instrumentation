# Design: pytest-threadlanes support in pytest-failure-instrumentation, the Sahara API and the Sahara UI

**Status:** the plugin half (§6.1) is **implemented** in pytest-failure-instrumentation 0.14.0. §6.2–§6.5 belong to the other repositories and are tracked there.
**Written:** 2026-09-24, from a working session on the pytest-lanes branch `claude/pytest-contract-backlog-722ycm`. **Amended:** 2026-09-25, to match what was built: pytest-lanes has since been renamed pytest-threadlanes, several facts below were corrected against the code, and the decisions that were open are recorded as taken (§9).
**For:** whoever maintains this support. Every file and function named below was read while writing this doc. Line numbers are from the commits listed in Appendix A, and may drift.

## 1. Context

`pytest-threadlanes` (module `pytest_threadlanes`, plugin name `threadlanes`; it was called pytest-lanes when this was first written) runs many tests at once as threads ("lanes") inside one process. It has three modes:

- `pytest --lanes 3`: one process with 3 lanes, named `ln0`, `ln1`, `ln2`.
- `pytest -n 2 --lanes 3`: 2 xdist processes, `gw0` and `gw1`, each with 3 lanes named `gw0.ln0` … `gw1.ln2`.
- `pytest -n 2`: plain xdist. This mode is unchanged by lanes, and must stay unchanged by this work.

`pytest-failure-instrumentation` ("the plugin") records what each xdist worker is doing and serves it live over HTTP (`/workers`, `/stack`). The Sahara API relays that to the Sahara web UI. All three assumed **one process = one worker = at most one test in flight**, and lanes break that assumption.

The plugin must not require pytest-threadlanes: it never imports it and does not depend on it. With it present, every lane is reported as a worker.

## 2. What went wrong before 0.14.0, and what it measures now

Measured end to end with the plugin at `9ecffa5` (0.13.1), pytest-threadlanes at `4b861df`, a live server (`--callstack-port`), and `-n 2`, `--lanes 3` and `-n 2 --lanes 3`. The scripts are in Appendix B.

| Area | Plain `-n` | With lanes, 0.13.1 | With lanes, 0.14.0 |
|---|---|---|---|
| `/workers` rows | one per worker, each with its test | **one row per process**: `main` (single process) or `gw0`/`gw1` (hybrid). Each names only **one** of its N running tests, whichever lane wrote last. The counters sum all lanes | one row per lane, each with its own test, phase and counts, plus `process`, `thread_name`, `thread_id`; no process row |
| `/stack?pid=` and `?worker=gw0` | the test's thread | ✅ every lane is its own thread, `lane-ln0` or `lane-gw0.ln0`, with correct frames | unchanged |
| `/stack?worker=ln0` and `?worker=gw0.ln0` | — | 404 | resolves to the lane's process |
| Stall incident: one test hangs 12s, siblings keep running, `failure_stall_seconds=3` | ✅ right test, right line | ❌ `--lanes`: `worker_stall main` blaming the **wrong test** (whichever lane wrote last), plus a false `STALLED_SILENT` for "worker `ln0`". ❌ Hybrid: `gw0` "no test running", with the stack in pytest-threadlanes' `_pump_events` | ✅ `ln0` / `gw0.ln0` blamed for `test_s[envA-0]` at `test_stall.py:6`, the stack of the lane's own thread, and no other incident |
| Worker death, hybrid | names the test | names one test, and only by luck of write order. Sibling lanes are not mentioned | one lane in flight: its test, "on lane gw0.ln1". Several: none blamed, all listed in `lanes_in_flight` |
| UI: a running test's Worker tab | ✅ | ❌ tests on N−1 of N lanes show "No worker in this run is on …", with no stack | lane rows match every running test (§6.3) |
| UI: stack view | opens `MainThread`, which is the test | opens `MainThread`, which is **pytest-threadlanes' scheduler**; the test's `lane-*` thread is collapsed further down | opens the lane's thread once the UI orders by `thread_id` (§6.3) |

A review of 0.14.0 at scale - 50 to 300 lanes, a process frozen or stopped, 120 lanes past faulthandler's hundred-thread cap, a free-threaded 3.14 - found what 0.14.1 fixes: a lane's stack could be a sibling's, a frozen process was one incident per lane each blaming its own test, idle lanes read their process's CPU as `working`, the heartbeat was seconds late beside busy lanes, a lane held a file descriptor for the session, and faulthandler's C timer could kill a worker of lanes. Each is described where it is fixed below (C1, C3, C4, C5, C6), and §6.6 lists what changed on the wire.

## 3. Goals and non-goals

**Goals:**
1. With lanes, every lane is visible and addressable as a worker. Its own test, phase and counters are in `/workers`, and a stall or death names the right test.
2. The Sahara API and UI **do not break** in any combination of old and new plugin, with and without lanes. That includes runs with no failure instrumentation at all.
3. Without lanes, the plugin's files and HTTP payloads are **identical** to 0.13.1's, and its existing test suite passes unchanged.
4. Only small, additive changes in the API and UI. All new wire fields are optional.

**Non-goals, for later:** grouping lanes under their process in the UI, per-lane profiling, per-lane memory figures (not measurable within one process), and crash-collateral annotation (pytest-threadlanes backlog, which this makes easy afterwards).

## 4. Facts about pytest-threadlanes the implementation relies on

The code is in `Heknon/pytest-threadlanes` under `src/pytest_threadlanes/`.

- **Lane identity comes through xdist's own API.** On a lane thread, `config.workerinput["workerid"]` is the lane (`ln3` or `gw0.ln3`), as are `worker_id` and `xdist.get_xdist_worker_id()`. That's touchpoint P10 in `isolation.py`, installed at `pytest_sessionstart`.
  - Elsewhere, `workerinput` is what it was: **absent** on the single-process main thread, and the **process's own** (`gw0`) on a hybrid worker's main thread.
  - At `pytest_configure`, which is when the plugin registers, it is not lane-aware yet.
- **Every test report carries `report.lane_id`,** for example `"gw0.ln3"`. It is a string and survives xdist serialization, so the hybrid controller sees it.
  - In single-process mode, `report.node` is also set to the lane (`ThreadNode`, with `.gateway.id == "ln3"`), as xdist sets it to the worker.
- **In single-process mode every lane is a node for xdist's controller hooks** (since `8f4dfd1`): `pytest_configure_node`, `pytest_testnodeready`, `pytest_xdist_node_collection_finished`, and `pytest_testnodedown(error=None)` **at the end of the run**, with a `ThreadNode` that has `.gateway.id`, `.workerinput`, `.workeroutput` and `.config`, and no `gateway._io`. So the engine already had `nodes`/`activity`/`collections` entries for each lane, `worker_of(report.node)` returned `"ln3"`, and `main` was armed once at session start and never touched again.
- **In hybrid mode the controller's scheduler schedules lanes,** not processes: its nodes are `LaneProxy` objects whose `gateway.id` is `gw0.ln3`, while xdist's `DSession` and every node hook see the real worker `gw0`.
- **Lane threads are named `lane-<lane id>`,** for example `lane-gw0.ln3`, and run the whole of each test: setup, call and teardown hooks. A lane is one thread for the whole session.
- Exclusive tests (`capsys` and the like) run on an extra lane, `ln-serial`, in single-process mode.
- pytest-threadlanes forces `--capture=no`, then captures stdout/stderr per lane itself. So **pytest does not touch fd 2 per test** under lanes.
- The hybrid worker's main thread runs pytest-threadlanes' `_pump_events` (`runner.py`).
- A process can be told it is running lanes with `config.getoption("lanes", None)` (an int, or None where the plugin is not installed; `0` means off). That is the only detection used: it works without importing the plugin.

## 5. How the plugin worked in 0.13.1 (the parts this touches)

The repo is `Heknon/pytest-failure-instrumentation`, `src/pytest_failure_instrumentation/`, version 0.13.1.

- **Registration** (`registration.py` ~L200–L255):
  - An xdist worker gets `WorkerRecorder(directory, config.workerinput["workerid"], …)`.
  - A single-process run gets `IncidentEngine` plus `WorkerRecorder(…, SOLE_WORKER="main", …)`.
  - A controller gets only the engine. `IncidentEngine.distributed` is `dist != "no"`, so `--dist X --lanes N` without `-n` would have recorded nothing.
- **`WorkerRecorder`** (`capture/recorder.py`) owns one `WorkerState` (`<worker>.state`, `capture/state.py`), one `EventLog` (`<worker>.events`, holding heartbeats) and a `Heartbeat` thread. It also owns a `SlowTestWatchdog`, an optional stderr tee and an optional profiler.
  - Per-test bookkeeping happens in `pytest_runtest_protocol` (resetting `_counted`/`_attempt`) and in `_phase()` (~L440–L535). That covers: `tests_started`/`finished`, `attempt`, `test_started`, `timeout_settings`, `state.update(nodeid, phase, …)`, `heartbeat.nodeid`/`.phase`, `slow_test.start_test`/`end_test`, `_tee_take`/`_tee_hand_back` (fd 2, also around `pytest_collection` and `pytest_make_collect_report`) and profiler boundaries. `pytest_internalerror` and `pytest_sessionfinish` read and clear the slot too.
  - **All of that is one-test-at-a-time.**
- **`/workers`** (`topology.run()` ~L183 globs `*.state`, and `topology.worker()` ~L224 builds one row per file):
  - The name is the file stem.
  - Heartbeat, CPU and RSS come from `<stem>.events`, and so do the run id (`_worker_run_id`), the heartbeat cadence (`_interval`, which re-reads the head of the file) and the finish (`_finish`).
  - `status`/`why` come from `_status(…)`: CPU rate from beats, process existence from the pid.
- **`/stack?worker=NAME`** (`stack_server.worker_pid` ~L896) globs `*/*.state`, matches the stem, and checks the pid is live. So **any `.state` file makes a name addressable**.
- **Stall detection** (`incidents/engine.py`):
  - `_touch(worker)` stamps `self.activity[worker]`. It is called from `pytest_runtest_logreport` using `worker_of(report.node)`, or `SOLE_WORKER` when there is no node.
  - `_watch_for_stalls` hands silent workers to `incidents/stall.build(worker, …)`. That reads `<worker>.events` (beats and CPU) and `<worker>.state` (in-flight nodeid), and gets a stack: py-spy of its own process where the pid is this one (`_own_stack`), otherwise a SIGUSR1 probe into `<worker>.crash`, falling back to `<worker>.slow`/`.frozen`/`.crash` (`_passive_stack`). `crash_stack._most_relevant` picks one thread out of a dump: "Current thread", else the first with the runtest protocol on it.
  - `_live_pid(worker)` asks xdist's gateway for the pid, and is None for a `ThreadNode`.
- **Worker death** (`incidents/death.py`, `build` and `recover`) reads `<worker>.state` for `test_in_flight`/`last_test`/`phase`/counters. Recovery is driven per `*.events` file (`leftovers.py`).
- **Other readers of `*.state`:** `resource_sampling._workers` (rows by pid), `incidents/killer.roles_in` (pid → name), `incidents/leftovers.worker_records`, and `death.recover_controller` (a pid set).
- **Wire models** (`client.py`): `Worker` (L175) and the rest are `_Wire` models with `extra="ignore"`. **A field that isn't declared on `Worker` is dropped** by every consumer that parses with these models, and that includes the Sahara API. `sampling.SampledWorker` is `extra="forbid"` and built field by field.

## 6. Design, as built

### Principle: a lane is a worker

Each lane gets **its own `.state` file, named after its lane id** (`ln3.state`, `gw0.ln3.state`), in the run directory beside the process's files. Everything that enumerates `*.state` or resolves names from state files (`/workers`, `/stack?worker=`, the UI's test→worker matching) then sees lanes as workers without being taught about them. Anything that is truly per-process (heartbeat, RSS, the pid, crash and watchdog files) stays on the **process's** files, and a lane's state file names its process so readers can find them. The helpers are in `lanes.py`, which imports nothing from pytest-threadlanes.

### 6.1 Plugin changes (0.14.0, as corrected in 0.14.1)

Every change is conditional on "a lane is running". A run without lanes takes the paths it took in 0.13.1 wherever lanes add a branch - the only unconditional changes are a lock around each event-log line and around the tee's copies, which change no byte - and it writes 0.13.1's bytes: `tests/test_lanes.py` compares a run's normalised files, state-slot bytes, event values, captured stderr, `/workers` rows, samples and payload JSON schemas with the ones 0.13.1 wrote (`tests/evidence_without_lanes.json`, recorded from 0.13.1 itself by `tests/without_lanes.py`).

**C1 · A state slot per lane** (`capture/recorder.py`, `capture/state.py`, `registration.py`, `lanes.py`)

- **Knowing there are lanes:** registration passes `lanes=lanes.requested(config)` to `WorkerRecorder`: pytest-threadlanes registered (`config.pluginmanager.hasplugin("threadlanes")`, its entry-point name) *and* `config.getoption("lanes", None)` truthy - an option whose dest is `lanes` alone can be any plugin's. Everything below is off when it is False.
- **The helper:** `_lane_of(item)` returns `getattr(item.config, "workerinput", None)["workerid"]` when that differs from `self.worker_id`, else `None` (absent on the single-process main thread; `gw0` on a hybrid worker's main thread).
- **The per-lane record:** a `dict[str, _Slot]`, where `_Slot` holds a `WorkerState`, the test it counted and the attempt, and for a lane its thread's native id and ident. The process has one slot of its own (`_own`), so `_phase` has one code path whatever the slot; a lane's is created lazily on its first test, on the lane's own thread. Each `WorkerState` is `WorkerState(directory / f"{lane}.state", os.getpid(), run_id, lane={…})`, and its record gains, after every existing key:
  - `"process"`: the recorder's `worker_id` (`"main"` or `"gw0"`),
  - `"thread_name"`: `threading.current_thread().name`,
  - `"thread_id"`: `threading.get_native_id()`,
  - `"thread_ident"`: `threading.get_ident()` — what faulthandler prints, so the stall and death readers can pick the lane's thread out of a dump (C4),
  - `"lane": true`.
  - (0.14.0 also kept the lane's CPU readings here, as `"cpu"`; 0.14.1 moved them to one file per process - C3.)
- **Routing:** every per-test mutation in `pytest_runtest_protocol` and `_phase()` goes to the lane's slot. A lane whose slot cannot be opened is recorded once (`lane_state_failed`) and runs unrecorded, never written into another slot. `pytest_internalerror` is usually called on the main thread - pytest-threadlanes re-raises a lane's error there - so it names the lane whose thread raised it if it is one, else the one lane still naming a test (the error interrupted it before the teardown that clears it), and where several are it lists them all (`lanes_in_flight` in the event) and names none; the incident then carries the lane's name as its worker. `pytest_sessionfinish` clears every lane's slot.
- **The process's own state:** the first lane slot writes `"lanes": true` into the **process's** record, whose node id is left alone from then on. That is how readers know the process is a container.
- **Descriptors:** a lane's `WorkerState` opens its file, writes it and closes it on each update, and holds no descriptor between them: one per lane for the session ended a run of 300 lanes at a 300-file limit (macOS' default soft limit is 256).
- **Thread safety:** a slot's counts and its `WorkerState` are only touched by its lane's thread. The dict is inserted into under a lock. `self.state` counters are never mutated from lane threads. `EventLog.record` now writes each line under a lock, since lanes, the heartbeat and the main thread all write it.

**C2 · `/workers` shows lanes, and the process row goes away when it has lanes** (`topology.py`, `client.py`, `sampling.py`, and the other readers)

- `topology.run()` skips a record with `"lanes": true`: its lanes are the rows. Asking for such a process by name (`/workers?worker=gw0`) lists its lanes, and the name is not reported unmatched. A process's event tail is read once per request for all its lanes. `stack_server.worker_pid` is **not** changed: the process's state still holds the pid, so `/stack?worker=gw0` still resolves, and lane states make `?worker=gw0.ln3` resolve.
- `topology.worker()`: a lane has no events file, so where `<stem>.events` has nothing to read, the state is asked whether it is a lane, and its `"process"` names the events file used for the beats, the run id, the cadence (`_interval` takes the events path now) and the finish. The name is refused unless it is a single plain component (`lanes.sibling`). `rss_mb`, `heartbeat_age_s`, `process_exists` and `status: finished` are the process's; `cpu_rate` is the lane's (C3).
  - The row gains `"process"`, `"thread_name"` and `"thread_id"` — only on a lane's row, appended at the end.
  - `tests_assigned`/`running`/`queued` and `rerunning` are the lane's own, and mean what a worker row's mean, for that lane: under `-n` xdist's scheduler hands work to lanes (pytest-threadlanes' `LaneMux` presents them to it), so the controller's `schedule.json` has a row per lane, and the engine counts finishes and reruns under `report.lane_id` to match. A run of lanes in one process has no xdist controller, no `dsession` for the engine to read the scheduler from, and so no schedule: there they are `None`, as in any run with no workers.
- `client.Worker` gains `process: Optional[str]`, `thread_name: Optional[str]` and `thread_id: Optional[int]`. `thread_id` is the native id, which a stack reports as `os_thread_id` — **not** a stack's `thread_id`, which is py-spy's handle.
- `sampling.SampledWorker` gains the same three, and omits them from its dump while they are unset, so a sample without lanes dumps exactly as before for a consumer holding the schema as a strict one.
- `resource_sampling._workers`, `killer.roles_in` and `leftovers.worker_records` skip lane states: a lane is a thread of a process listed under its own name with the same pid. The resource sampler counts, for a container process, how many of its lanes have a test in flight, and serves it as `lanes_running` on that process's worker row (`client.ResourceProcess.lanes_running: Optional[int]`), absent for every other process.
- `SampledWorker` and `WorkerDeathIncident` leave their lane-only fields out of a dump while unset through one helper, `lanes.without_unset`, called from a wrap serializer with no return annotation - an `-> Any` there made pydantic render the model's serialization schema as `{}`. The schemas are 0.13.1's plus only the declared new properties, and the suite checks it.

**C3 · Per-lane CPU, so a hung lane in a busy process reads as blocked** (`capture/heartbeat.py`, `capture/recorder.py`, `lanes.py`, `probes/process.py`, `analysis/stall.py`)

- The recorder hands the heartbeat `lane_cpu=`, called after each beat: it reads every lane's thread CPU at one instant and writes them all into one file of the process, `<process>.lanecpu` - `{"run_id", "times": [...], "lanes": {lane: [seconds or null, ...]}}`, the newest six readings, written whole to a `.part` file and renamed over it (`lanes.LaneCpu`; read back by `lanes.lane_cpu`, which refuses another run's file). The beat line is unchanged. (A first version put a `threads` map on the beat itself; at 1,000 lanes that was 14 KB a beat, the 64 KB tail readers take held four beats, and past 3,000 lanes not one whole beat. 0.14.0 wrote each lane's readings onto its own record: one `pwrite` per lane per beat on the heartbeat's thread, and a lane's `time` moved with every beat - E1, S1.)
- **Read without letting go of the GIL.** On Linux each thread's CPU clock has a clock id made from its thread id (`MAKE_THREAD_CPUCLOCK`, what `pthread_getcpuclockid` returns), and `time.clock_gettime` reads it in one system call that keeps the GIL (`probes.process.thread_cpu_seconds`). `psutil.Process().threads()` reads a `/proc` file per thread in Python, each read releases the GIL, and each time the heartbeat has to win it back from every running lane: beside twenty lanes running Python on four cores, one reading took 17.7 s and the beat was 19 s late. The slot list is copied without the lanes' lock for the same reason - a lane opening its slot holds it across file I/O - since slots are only ever added. Windows uses psutil, which numbers threads by native id there.
- On macOS psutil numbers threads by position, not by native id, so no lane can be found: the recorder does not ask there and records `lanes_adjusted` (`per_lane_cpu`). No `.lanecpu` is written; a lane's row has no rate, and its stall verdict is judged on its process's CPU and says so.
- `analysis.stall.lane_beats(beats, readings, interval)` turns a lane's readings into beats every rule reads CPU from. It is used for a lane's row (`topology`) and its stall verdict (`incidents/stall.py`). A lane with fewer than two readings, or whose readings stopped while the beats went on (its thread has ended), has none: its `cpu_rate` is null and never its process's, which read an idle lane beside a busy one as `working`. A lane with no test in flight reads null as well, whatever its thread last burned, and its `why` says it has no test in flight.
- **Whether the file is current** (`analysis.stall.lane_cpu_current`): it holds a reading at least as new as the beat before the newest. Each beat's readings are written after the beat, and fifty lanes running Python kept the heartbeat from the GIL for up to three seconds a beat at a one-second interval, so "within an interval of the newest beat" called a current file stale for a moment after each beat. A file that is not current - its writes failing on a full descriptor table, or on a file another program holds open on Windows - is read as no file: a lane with a test in flight then reads its process's figure, as a process writing no file does (macOS), and its `why` and stall evidence say the figure is the process's. A stall assessed before a lane's second reading waits for the next poll only while the file is current and the lane's phase began within three beats; before, a file whose writes had stopped deferred a hung lane forever.
- **A saturated process** (`analysis.stall.fair_share`): where the process burns at least 0.8 cores (`SATURATED_CORES`) and more than one lane has a test in flight, a lane is working above a quarter of its fair share - the process's rate over its lanes in flight - rather than above the fixed `BUSY_THRESHOLD` of 0.05 cores; fifty CPU-bound lanes read 0.036-0.048 cores each and were all reported blocked at high confidence. A lane waiting on something beside them burns nothing and is still reported, with the saturation in its reason. Where the share adds up to less than 0.05 CPU seconds over the window (`RESOLVABLE_CPU_SECONDS`, ten 5 ms switch intervals; a thousand lanes running Python), the verdict is low confidence. The same threshold decides a lane row's `working`/`blocked`.
- The recorder reads a lane's CPU only while its thread is alive (`_Slot.alive`, a weak reference to the thread), since an ended thread's native id can be given to a new one.

**C4 · Stall detection per lane** (`incidents/engine.py`, `incidents/stall.py`, `capture/crash_stack.py`)

- `IncidentEngine.pytest_runtest_logreport`: when `report.lane_id` is present, `_touch_lane(lane, process)` touches the **lane** and its **process** (`main` where the run records here, else `worker_of(node)`), and remembers the lane's process. The schedule bookkeeping is keyed by the lane too (C2).
- Reports are not enough to time a lane: under `-n` the controller hears of a lane only from its first report, so a hang in the setup of a lane's first test went untimed, even with every lane wedged; and pytest-threadlanes holds an exclusive test's reports until it ends, so its phases read as one silence. So each poll of the stall watcher also reads the lanes' own records (`_follow_lanes`), arms every lane with a test in flight, and takes its clock from the start of its current phase (`phase_started`, written on the lane's thread) where that is later than its last report.
- The process is the container of its lanes, and `stall.build` judges it only when none of its lanes has a test in flight (then as any worker with no test running: low confidence, saying its lanes were idle). This removed the false `main` incident, and the hybrid `gw0` "no test running … `_pump_events`".
- A lane with no test in flight is idle — never given one (more lanes than work), out of work at the end of an uneven run, or waiting — and `stall.build` returns None for it (the engine re-arms it). A lane the engine knows (`known_lane`) that has no record at all is idle too. An idle lane never raises anything.
- For a lane in flight, `stall.build` reads beats from `<process>.events` with the lane's own CPU and the in-flight test from the lane's state. The stack is **the lane's thread or nothing**: in a single-process run it is read from the lane's own frames (`sys._current_frames()`, `crash_stack.from_frame`, `stack_source: "frames"`), which works past a hundred threads and on a free-threaded interpreter py-spy cannot read; in an xdist worker py-spy reads the worker's pid once per poll however many of its lanes are silent (the engine's per-poll `shared` cache), falling back to one SIGUSR1 probe per pid per poll. `crash_stack.read(…, thread=(ident, name))` and `from_threads(…, thread=…)` return nothing when the thread is not in the dump - faulthandler writes at most a hundred threads - rather than a sibling's stack, and the header names the lane's thread. The incident's evidence names the lane's thread and process, says the CPU figure is that thread's own ("the lane's thread used 0.00 cores"), mentions the siblings only when some have a test in flight, and in a single-process run says the run waits on this lane's test rather than on xdist.
- **A frozen process is one incident, the process's.** The process's heartbeat is checked before any lane's CPU: when it has stopped (and stays stopped on confirmation), `stall.build` returns one `STALLED_FROZEN` incident for the process (`worker: "gw0"`), listing every lane in flight in `lanes_in_flight` (omitted from the dump when empty), and the engine marks the process and all its lanes stalled. A lane is blamed only when py-spy finds its thread holding the GIL; a process stopped by a signal (SIGSTOP, a debugger) blames none. 0.14.0 raised one FROZEN incident per lane, each blaming its own innocent test (B2).
- **Starved, not frozen.** Under lanes a heartbeat can be late because its process's lanes are running Python and keeping it from the GIL - a late beat looks like a frozen one, and at a one-second interval fifty busy lanes produced a false `STALLED_FROZEN`. So the freeze is confirmed from the lanes' threads too: their CPU is read from outside the process (`probes.process.process_thread_cpu`, by native id) before and after the confirmation wait, and a process whose lanes shared that CPU - no one thread holding 80% of it - is taking turns at the GIL, not frozen, and nothing is raised. A frozen process runs one thread (native code holding the GIL) or none (stopped); lanes waiting on it can be charged a clock tick for asking for the GIL, which is why the spread decides rather than whether a second thread moved. Where threads cannot be read by native id (macOS) the freeze stands.
- **After a freeze.** The engine keeps the lanes a process-level incident accounts for apart from `stalled` (`IncidentEngine.silenced`), and once the process beats again it hands them back to the watch with their clocks restarted: a lane hung on its own before and after the freeze is then reported, which it never was when the freeze put every lane in `stalled`.
- **Probing off.** `failure_stack_probe = false` is honoured for a worker of lanes: py-spy pauses the process it reads, and the setting is a promise to leave workers alone. A lane's stack is then whatever its process dumped on its own, or none, with the reason. A single-process run reads its own frames, which disturbs nothing.
- **One read per process per poll.** A lane's assessment reads its process's lane records and `.lanecpu` through the poll's shared cache: at a thousand lanes each listing every record, one poll was a million state reads (0.55-1.1 s a lane); it is now about a second for all of them.
- `_stopped` takes only a process stopped by a signal as stopped: one in tracing-stop is being read by py-spy (this engine's read, or the live view's).
- The lane's record is read again after its stack is taken, and a lane whose test changed meanwhile raises nothing: that stack is not the test's.
- `_live_pid(lane)` falls back to the node of the lane's process (`gw0`).
- `pytest_testnodedown(gw0)` also takes `gw0`'s lanes out of the watch and marks them down; a stall of a lane whose process already has a death report is suppressed as a process's would be.
- `--dist X --lanes N` without `-n` or `--tx` is a single-process run: `IncidentEngine.distributed` is False when `lanes.in_one_process(config)`.

**C5 · Worker death names each lane's test** (`incidents/death.py`)

- When a process worker with `"lanes": true` dies, `_through_lanes` reads every `*.state` whose `"process"` is that worker (listing the directory; never building a path from a name).
- The counts are always the process's: every lane's, summed.
- Exactly one lane in flight: that lane's test, phase, clocks and timeouts are the worker's, as a process's always were, and the summary says "on lane gw0.ln1".
- A fatal dump whose "Current thread" names a thread (`crash_stack.current_thread`, matched on `thread_ident`; a native fault is delivered to the thread that faulted) decides it whatever the count: that lane's test is blamed the same way, the others stay in `lanes_in_flight`, and the evidence says they went down with the process; a thread that is none of theirs - one a test left running - blames no lane, even when one lane is in flight (N1).
- A free-threaded interpreter's fatal dump names no thread (`<Cannot show all threads while the GIL is disabled>`, then one `Stack (most recent call first):`): the incident's stack is that Python stack (`crash_stack.free_threaded` - `read` would return the C stack trace 3.14 prints after it, whose header begins "Current thread"), and the lane whose test function is on it is blamed where exactly one is; where several lanes run that function, or none, the evidence says so and none is blamed (S6a). The frames name a function, not a parametrization, so lanes running the same parametrized test - the usual shape of a run of lanes, one environment per lane - cannot be told apart this way.
- Several otherwise: `test_in_flight` and `last_test` are `None`, `suspect_nodeid()` is None, and the summary and evidence list them. None: the last test is the most recently written lane's.
- `lanes_in_flight: list[{lane, nodeid, nodeid_hash, phase}]` lists every lane in flight. It is a declared field, omitted from the dump when empty, so a death without lanes dumps exactly as before.
- The recovery path (`death.recover`, driven per `*.events` file) does the same through the process's events.

**C6 · Process-wide machinery that must not run per test under lanes** (`capture/recorder.py`, `capture/output.py`, `capture/crash_stack.py`)

Each is recorded as a `lanes_adjusted` event (`mechanism`, `action`, `reason`) in the process's event log:

- **Stderr tee (fd 2):** taken once, at session start, and handed back at session finish; `_tee_take` does nothing under lanes. What arrives is passed on to the original stderr by `StderrTee.drain()`, at every lane's phase end and on every heartbeat tick, under one lock. The disk under what was passed on is given back as it goes (`StderrTee._release`, once the file is two rings past the last release): on Linux by punching a hole (`fallocate(PUNCH_HOLE | KEEP_SIZE)`) over all but the last ring, which keeps the one open file description every writer of fd 2 shares; on a Linux filesystem that cannot punch holes by rotating (`_rotate`: `dup2` a fresh file onto fd 2, drain the old one to its end and keep following it on every drain until the next rotation (a write another thread began before the switch lands there late on a loaded machine; one more drain lost such a line in CI), keep its last ring as `<name>.output.prev`, which `read_tail` reads ahead of the current file). Rotation was what review asked for, and it is exact for this process's writes - but a test's child process holds its own inherited copy of fd 2, which no `dup2` here can reach, and keeps writing the old file after it is closed: measured, 3 MiB of 20 lost on the way to the terminal. A hole moves nothing, and lost nothing: 20 MiB through, 16 KiB on disk. Off Linux nothing is given back and the file keeps the session's stderr (`output.ROTATES`): rotating swaps fd 2 under the lanes writing it, which macOS does not do in one step - CI's macOS suite lost everything one writer thread wrote after a rotation. Every capture file of a session is opened `O_APPEND`, since macOS does not serialize writes at a shared offset, and drains pass on whole lines only, up to where the current file ended before the retired one was read.
- **Stderr tee at session end:** once fd 2 is handed back, `StderrTee.compact()` rewrites the file to its last ring (`.part`, then renamed over it), so it is not left sparse with an apparent size of the whole session's stderr (S3).
- **`SlowTestWatchdog`:** one clock per lane (`start_lane`/`end_lane`), ticking on each lane's own cadence; the dump is of every thread, so at most one is taken per process per timeout, and every lane overdue at that moment is marked dumped by it (200 staggered lanes took 11 dumps of 360 KB in 50 s before, N3). It is discarded only once no running lane is overdue.
- **Frozen-interpreter fallback:** off under lanes (`lanes_adjusted`, `frozen_fallback`). faulthandler's C timer dumps without the GIL and is safe only once no Python thread is executing; three missed beats meant that with one test in flight, but not with lanes: twenty lanes running Python kept the heartbeat from the GIL past the deadline, and a stopped process resumes every lane at the instant the overdue timer fires - the dump then walked frames being torn down and the worker died of SIGSEGV (3 runs of 4, and 5 of 5 after SIGSTOP/SIGCONT). A frozen process of lanes gets its stack from py-spy with its incident instead.
- **Profiler:** disabled under lanes.
- **Heartbeat:** the process's beat carries no nodeid under lanes, so memory observers get no test attribution (memory is per process). Per-lane CPU is in the process's `.lanecpu` file (C3).

**C7 · Version:** `pyproject.toml` `version` and `__init__.__version__` were **0.14.0**, a minor bump: new optional fields and behaviour, no breaking change; **0.14.1** carries review round 2's fixes (§6.6). The README section "Running under pytest-threadlanes" covers the per-lane rows, the new fields, per-lane CPU and stalls, deaths, and what C6 changes.

### 6.2 Sahara API

The mock and reference implementation is `Heknon/morphine-sahara-mock-api`. `ingest/live_view.py` says it is "written to be copied into the production API". It **imports and extends** the plugin's models (`WorkerRecord(Worker)` at ~L176), so C2's new fields pass through with **no code change**. The only change is the dependency prose: `pytest-failure-instrumentation>=0.14.0` in `ingest/README.md` and the docstrings (the mock has no pin file; the production API is out of scope here).

To verify, run `ingest/verify_live_view.py` against a run with lanes, and confirm that `process`, `thread_name` and `thread_id` reach the JSON; add a lanes fold to `check_folds`.

### 6.3 Sahara web UI (`Heknon/morphine-sahara-web`)

**Nothing is required for it not to break.** Lane rows are ordinary `WorkerRecord`s:
- `findWorkerForTest` (`pages/DashboardPage/CycleOverview/workerState/workerStatus.ts` ~L80) finds every running test.
- Rows are keyed by `server_id/worker` (`workerState/servers.ts` ~L78), which stays unique.
- Stacks are keyed by pid (`TestDetails/tabs/Worker/getCallstack.ts` ~L58), so lanes of one process share one read. That's correct.

The small change that makes it *right*:
1. **Types:** `src/types/worker.ts` `WorkerRecord` gains `process?: string | null`, `thread_name?: string | null` and `thread_id?: number | null`, each documented. **Naming hazard:** `WorkerRecord.thread_id` is the native id and matches `CallstackThread.os_thread_id`, not `CallstackThread.thread_id` (py-spy's handle).
2. **Carrying the thread:** `getCallstack.ts` `WorkerProcess` gains an optional `threadId?: number | null`, set from `record.thread_id ?? null` in `workerProcess(record)`. **Don't** add it to the query key; the stack is per process. `Workers/Resources/ProcessTable.tsx` builds a process-level `WorkerProcess` and must keep `MainThread` first.
3. **Thread order:** `tabs/Worker/threadOrder.ts` `orderThreads(threads, pid, focusThreadId?)` puts the thread whose `os_thread_id === focusThreadId` first. Otherwise `MainThread` stays first, as today.
4. **Wiring:** `CallstackThreads.tsx` already opens index 0, so ordering is enough. `CallstackSection` is its own file, `tabs/Worker/CallstackSection.tsx` (call sites `Worker.tsx` and `Workers/WorkerDetail.tsx`). `callstackDiff.ts` `compareCallstacks(readings)` needs a focus parameter (its docstring's "the main thread" lead needs rewording), and `callstackText.ts` `callstackAsText(callstack)`, called from `CallstackToolbar.tsx`, gets the focus through the toolbar.
5. **Tests:** cases beside the existing `threadOrder.test.ts`, `callstackDiff.test.ts`, `callstackText.test.ts` and `workerStatus.test.ts` (a new `*.test.ts` must be added to `package.json`'s `test` list). A lane record with a shared pid and `thread_id` must open its `lane-*` thread; a record without `thread_id` must behave exactly as today.

The Resources view needs no change: it never joins to `WorkerRecord`, and its collector names processes `gw0`.

### 6.4 Mock API (`Heknon/morphine-sahara-mock-api`)

Add **one lanes-shaped run** so the UI can be developed and screenshotted without a real run:
- `findCycleWorkers` gives every cycle the same two runs, so the lanes run goes on a **dedicated cycle** (or server) only, keeping all existing data unchanged: 2 processes × 3 lanes, worker names `gw0.ln0` … `gw1.ln2`, shared pids per process, `process: "gw0"`, `thread_name: "lane-gw0.ln1"` and `thread_id` set.
- Stacks are keyed by nodeid (`callstack.ts findTestCallstack`), and the route in `src/routes/cycle.ts` resolves pid → first worker → nodeid. A lanes process needs a pid-keyed stack builder — `MainThread` in `_pump_events`, plus one `lane-*` thread per lane in its own test's frames, `os_thread_id` equal to the lane's `thread_id` — and a route branch.
- Update the types in `src/types.ts` (`WorkerRecord`).

### 6.5 pytest-threadlanes

No change is required. The end-to-end check in Appendix B runs in this plugin's suite (`tests/test_lanes.py`, skipped where pytest-threadlanes is not installed), in all three modes; it could be mirrored in pytest-threadlanes' contract tests as a test that runs only when pytest-failure-instrumentation is installed.

### 6.6 What 0.14.1 changed on the wire and on disk

- `<process>.lanecpu` is new; lane state records no longer carry `"cpu"`; a lane's record `time` moves only at phase transitions, so `state_age_s` is its phase's age (E1).
- `WorkerStallIncident.lanes_in_flight` - `[{lane, nodeid, nodeid_hash, phase}]` - on a process-level FROZEN incident, omitted from the dump when empty.
- `/resources`: a process hosting lanes has `nodeid: null` (0.14.0 sent `""`); every other row is unchanged (e2e-2).
- `client.Worker` omits `process`, `thread_name` and `thread_id` from `model_dump()` while unset, as `SampledWorker` does, so `client.WorkersSnapshot` dumps as 0.13.1's for a run without lanes (A1). Its JSON schema is unchanged.
- A lane row's `cpu_rate` is null where the lane has no test in flight or no readings of its own in a current `.lanecpu`, and its process's figure - said as such in `why` - where the process writes no `.lanecpu` or its writes stopped. A lane of a saturated process is `working` above a quarter of its fair share.

## 7. Compatibility

| Run | Plugin output | Sahara API | Sahara UI |
|---|---|---|---|
| No lanes, new plugin | identical to 0.13.1 (files, `/workers`, samples, incident dumps) | unchanged | unchanged |
| Lanes, old plugin (≤ 0.13.1) | the degraded rows of §2 | unchanged | as today (degraded) |
| Lanes, new plugin, old API pin | lane rows; the new fields are dropped by the old models | works | lane rows work; stack opens on `MainThread` |
| Lanes, new plugin, new API | lane rows plus `process`/`thread_*` | passes fields through | lane rows, stack opens on the lane's thread |
| No failure instrumentation | — | existing `no_server` answer | existing "unavailable" state |

Parsed by `client.Worker`, a row without lanes has the three new fields as `None`, and `model_dump()` leaves them out (0.14.1; 0.14.0 dumped them as null); the payload the plugin serves does not carry them.

## 8. Implementation plan and where each step landed

Write the failing test first each time. **Run tests locally: GitHub runners are paid.** The plugin repo's `AGENTS.md` explains its CI policy. Use `pytest -n 4` locally.

1. **Plugin, C1 + C2** — done. `tests/test_lanes.py`: rows per lane and no process row (fed evidence, and live under `--lanes 3` and `-n 2 --lanes 3`), `?worker=gw0.ln1` resolves, and the run without lanes compared with 0.13.1's evidence.
2. **Plugin, C4 + C3** — done. The stall scenario blames `test_s[envA-0]` at its line in all three modes, with nothing else raised, and idle lanes beside a busy one raise nothing.
3. **Plugin, C5** — done. A hybrid death with sibling lanes in flight lists them; with the culprit alone it names it.
4. **Plugin, C6** — done. Under lanes the events say fd 2 is taken once, the watchdog is per lane and the profiler is off; the stderr still reaches the terminal.
5. **Plugin, C7** — done. 0.14.0 in both places, and the README section.
6. **Mock API (§6.4),** then **UI (§6.3)**, with unit tests. Check in the browser against the mock's lanes run.
7. **API (§6.2):** the dependency prose, and `verify_live_view.py`.
8. **pytest-threadlanes (§6.5):** optional mirror of the end-to-end check.

## 9. Decisions

Taken, and not to be re-litigated without the owner:

- `/workers` **hides** the process row when that process has lanes; the lanes are the rows.
- The profiler is **disabled** under lanes, with an event.
- Lane rows carry per-lane progress through the existing fields (`tests_assigned`, `tests_running`, `tests_queued`, `rerunning`), meaning what a worker row's mean, for that lane - where the controller's schedule has lane rows (`-n`). No new fields. A run of lanes in one process has no schedule, and those fields are `None` there.
- `--dist X --lanes N` without `-n` is a single-process run that records here.
- Memory high-water marks under lanes carry no test attribution.

## Appendix A: repositories and commits read

| Repo | Commit | Notes |
|---|---|---|
| `Heknon/pytest-failure-instrumentation` | `9ecffa5` | version 0.13.1; 0.14.0 built on it |
| `Heknon/morphine-sahara-mock-api` | `a87697a` | `ingest/live_view.py`, `src/data/cycles/*` |
| `Heknon/morphine-sahara-web` | `ab160bb`, re-checked at `69dd339` | paths under `src/` |
| `Heknon/pytest-threadlanes` | `4b861df` (first read as pytest-lanes, branch `claude/pytest-contract-backlog-722ycm`) | P10 lane identity, `report.lane_id`, `lane-<id>` thread names, node hooks per lane (`8f4dfd1`) |

## Appendix B: the end-to-end scenarios

All use this conftest (the user's scheduling pattern, with one environment per lane):

```python
# conftest.py
import json
from xdist.scheduler import LoadScopeScheduling
class EnvScheduling(LoadScopeScheduling):
    def _split_scope(self, nodeid):
        return nodeid.rsplit("[", 1)[1].split("-", 1)[0]
def pytest_xdist_make_scheduler(config, log):
    return EnvScheduling(config, log)
def pytest_failure_incident(incident):
    with open("incidents.jsonl", "a") as f:
        f.write(json.dumps({"kind": incident.kind, "worker": incident.worker, "text": str(incident)}) + "\n")
```

On Python 3.13 and earlier pytest-threadlanes needs `-p no:warnings`.

**Live view.** Six environments × 2 steps, each test sleeping 4s inside a function named after its environment (`work_envA` …). While it runs, `GET /workers`, `GET /stack?worker=<each>`, `GET /stack?pid=<each>` and `GET /stack?worker=gw0.ln0`. Run with:

```
pytest -p no:warnings -p no:cacheprovider <mode> --failure-instrumentation --callstack-port 18765 -o failure_directory=evidence
```

Measured with 0.14.0: one row per lane with its own `nodeid` (3 of 3 under `--lanes 3`, 6 of 6 under `-n 2 --lanes 3`), each row's `thread_id` found as an `os_thread_id` in `/stack?worker=<lane>` with that lane's `work_env*` frame on it, sleeping lanes `blocked` at `cpu_rate` 0.0, and `?worker=gw0.ln0` resolving. `-n 2` unchanged.

**Stall.** With `failure_stall_seconds = 3`:

```python
@pytest.mark.parametrize("step", range(16))
@pytest.mark.parametrize("env", ["envA", "envB", "envC", "envD", "envE", "envF"])
def test_s(env, step):
    if env == "envA" and step == 0:
        time.sleep(12)          # hung: blocked, no CPU
    elif env != "envA":
        time.sleep(0.5)
```

- 0.13.1: `-n 2` gives `worker_stall gw0 … test_s[envA-0] … test_stall.py:6`, which is correct. `--lanes 3` gives `worker_stall main` blaming the wrong test plus `ln0 STALLED_SILENT`, and `-n 2 --lanes 3` gives `gw0` "no test running … in _pump_events".
- 0.14.0: `-n 2` as before; `--lanes 3` gives `worker_stall ln0 … test_s[envA-0] (call) … in test_s (test_stall.py:6)`; `-n 2 --lanes 3` gives the same for `gw0.ln0`. No other stall incident in any mode.

**Idle lanes at the end of an uneven run.** One environment burns a core for 6s while the others finish in a second and wait, with `failure_stall_seconds = 2`: no incident under `--lanes 3` or `-n 2 --lanes 3`.

**Death (hybrid).** Three environments × 2 steps. `envB` step 0 calls `os._exit(1)` once, using a flag file under `tmp_path_factory.getbasetemp().parent`. Run with `-n 1 --lanes 3`. Measured with 0.14.0: with its siblings mid-test, `Worker gw0 exited on its own with code 1 while running 3 tests at once, on lanes gw0.ln0, gw0.ln1, gw0.ln2`, each listed in `lanes_in_flight` and none blamed; with the siblings already done, `… while running test_d[envB] (call) on lane gw0.ln1`. xdist replaces the worker and the run completes.

## Appendix C: cross-check with `--lanes-detect`

pytest-threadlanes' shared-state detector (`pytest --lanes-detect`) was run on a small suite with `--failure-instrumentation` active. With no hints, it reported this per-test state in the plugin's objects. Every item is covered by §6.1:

| Reported path | What it is | Covered by |
|---|---|---|
| `plugin:failure-instrumentation-recorder._counted`, `._attempt` | per-test bookkeeping in `WorkerRecorder` | C1 (lane slot) |
| `…-recorder._open_resources[0].nodeid` / `.phase_started` / `.attempt` / `.tests_started` / `.tests_finished` / `.test_started` / `.sequence` / `.last_nodeid*` / `._hashed` | the process's `WorkerState` record (reached through `_open_resources`) | C1 (a `WorkerState` per lane) |
| `…-recorder.heartbeat._identity` | the heartbeat's single nodeid/phase | C6 (no nodeid on the process beat) and C3 |
| `…-recorder.heartbeat.tickers[0]._started_at` | `SlowTestWatchdog`, one clock per process | C6 (per-lane clock) |
| `plugin:failure-instrumentation-controller.activity['main']` | stall detection keyed by `SOLE_WORKER` | C4 (key by `report.lane_id`; `main` judged only with no lane in flight) |

**How to use the detector here:** a sequential run still reports the process-level state (without lanes the plugin writes the process's own slot, exactly as before), so the detector is not the acceptance test for this work; the three-mode end-to-end scenarios in Appendix B are. It is useful as a regression check: any *new* per-test state in the plugin shows up in its report. Run it as `pytest --lanes-detect --lanes-detect-report=shared.json --failure-instrumentation`, and compare `findings` with this table.
