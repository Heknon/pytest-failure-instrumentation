# Design: pytest-lanes support in pytest-failure-instrumentation, the Sahara API and the Sahara UI

**Status:** ready to implement. Nothing described here has been built yet.
**Written:** 2026-09-24, from a working session on the `pytest-lanes` branch `claude/pytest-contract-backlog-722ycm`.
**For:** the agent that implements it. Every file and function named below was read while writing this doc. Line numbers are from the commits listed under "Repositories", and may drift.

## 1. Context

`pytest-lanes` runs many tests at once as threads ("lanes") inside one process. It has three modes:

- `pytest --lanes 3`: one process with 3 lanes, named `ln0`, `ln1`, `ln2`.
- `pytest -n 2 --lanes 3`: 2 xdist processes, `gw0` and `gw1`, each with 3 lanes named `gw0.ln0` … `gw1.ln2`.
- `pytest -n 2`: plain xdist. This mode is unchanged by lanes, and must stay unchanged by this work.

`pytest-failure-instrumentation` ("the plugin") records what each xdist worker is doing and serves it live over HTTP (`/workers`, `/stack`). The Sahara API relays that to the Sahara web UI. All three assume **one process = one worker = at most one test in flight**, and lanes break that assumption.

## 2. What goes wrong today

Measured end to end with the plugin at `9ecffa5`, a live server (`--callstack-port`), and `-n 2`, `--lanes 3` and `-n 2 --lanes 3`. The scripts are in Appendix B.

| Area | Plain `-n` | With lanes |
|---|---|---|
| `/workers` rows | one per worker, each with its test | **one row per process**: `main` (single process) or `gw0`/`gw1` (hybrid). Each names only **one** of its N running tests, whichever lane wrote last. The counters sum all lanes |
| `/stack?pid=` and `?worker=gw0` | the test's thread | ✅ every lane is its own thread, `lane-ln0` or `lane-gw0.ln0`, with correct frames |
| `/stack?worker=ln0` and `?worker=gw0.ln0` | — | 404 |
| Stall incident: one test hangs 12s, siblings keep running, `failure_stall_seconds=3` | ✅ right test, right line | ❌ `--lanes`: blames the **wrong test**, and also raises a false `STALLED_SILENT` for "worker `ln0`". ❌ Hybrid: "no test running", with the stack in pytest-lanes' `_pump` |
| Worker death, hybrid | names the test | names one test, and only by luck of write order. Sibling lanes are not mentioned |
| UI: a running test's Worker tab | ✅ | ❌ tests on N−1 of N lanes show "No worker in this run is on …", with no stack |
| UI: stack view | opens `MainThread`, which is the test | opens `MainThread`, which is **pytest-lanes' scheduler**; the test's `lane-*` thread is collapsed further down |

## 3. Goals and non-goals

**Goals:**
1. With lanes, every lane is visible and addressable as a worker. Its own test, phase and counters are in `/workers`, and a stall or death names the right test.
2. The Sahara API and UI **do not break** in any combination of old and new plugin, with and without lanes. That includes runs with no failure instrumentation at all.
3. Without lanes, the plugin's files and HTTP payloads are **identical** to today's, and its existing test suite passes unchanged.
4. Only small, additive changes in the API and UI. All new wire fields are optional.

**Non-goals, for later:** grouping lanes under their process in the UI, per-lane profiling, per-lane memory figures (not measurable within one process), and crash-collateral annotation (pytest-lanes backlog item 6, which this makes easy afterwards).

## 4. Facts about pytest-lanes the implementation relies on

The code is in `Heknon/pytest-lanes` under `src/pytest_lanes/`.

- **Lane identity comes through xdist's own API.** On a lane thread, `config.workerinput["workerid"]` is the lane (`ln3` or `gw0.ln3`), as are `worker_id` and `xdist.get_xdist_worker_id()`. That's touchpoint P10 in `isolation.py`, installed at `pytest_sessionstart`.
  - Elsewhere, `workerinput` is what it was: **absent** on the single-process main thread, and the **process's own** (`gw0`) on a hybrid worker's main thread.
  - At `pytest_configure`, which is when the plugin registers, it is not lane-aware yet.
- **Every test report carries `report.lane_id`,** for example `"gw0.ln3"`. It is a string and survives xdist serialization, so the hybrid controller sees it.
  - In single-process mode, `report.node` is also set to the lane (`ThreadNode`, with `.gateway.id == "ln3"`), as xdist sets it to the worker. This is why the plugin's engine currently thinks `ln3` is an xdist worker (§2).
- **Lane threads are named `lane-<lane id>`,** for example `lane-gw0.ln3`, and run the whole of each test: setup, call and teardown hooks.
- Exclusive tests (`capsys` and the like) run on an extra lane, `ln-serial`, in single-process mode.
- pytest-lanes forces `--capture=no`, then captures stdout/stderr per lane itself. So **pytest does not touch fd 2 per test** under lanes.
- A process can be told it is running lanes with `config.getoption("lanes", None)` (int or None). `config.pluginmanager.hasplugin("pytest_lanes.plugin")` also works.

## 5. How the plugin works today (the parts this touches)

The repo is `Heknon/pytest-failure-instrumentation`, `src/pytest_failure_instrumentation/`, version 0.13.1.

- **Registration** (`registration.py` ~L200–L255):
  - An xdist worker gets `WorkerRecorder(directory, config.workerinput["workerid"], …)`.
  - A single-process run gets `IncidentEngine` plus `WorkerRecorder(…, SOLE_WORKER="main", …)`.
  - A controller gets only the engine.
- **`WorkerRecorder`** (`capture/recorder.py`) owns one `WorkerState` (`<worker>.state`, `capture/state.py`), one `EventLog` (`<worker>.events`, holding heartbeats) and a `Heartbeat` thread. It also owns a `SlowTestWatchdog`, an optional stderr tee and an optional profiler.
  - Per-test bookkeeping happens in `pytest_runtest_protocol` (resetting `_counted`/`_attempt`) and in `_phase()` (~L440–L535). That covers: `tests_started`/`finished`, `attempt`, `test_started`, `timeout_settings`, `state.update(nodeid, phase, …)`, `heartbeat.nodeid`/`.phase`, `slow_test.start_test`/`end_test`, `_tee_take`/`_tee_hand_back` (fd 2) and profiler boundaries.
  - **All of that is one-test-at-a-time.**
- **`/workers`** (`topology.run()` ~L183 globs `*.state`, and `topology.worker()` ~L224 builds one row per file):
  - The name is the file stem.
  - Heartbeat, CPU and RSS come from `<stem>.events`.
  - `status`/`why` come from `_status(…)`: CPU rate from beats, process existence from the pid.
- **`/stack?worker=NAME`** (`stack_server.worker_pid` ~L896) globs `*/*.state`, matches the stem, and checks the pid is live. So **any `.state` file makes a name addressable**.
- **Stall detection** (`incidents/engine.py`):
  - `_touch(worker)` stamps `self.activity[worker]`. It is called from `pytest_runtest_logreport` using `worker_of(report.node)`, or `SOLE_WORKER` when there is no node.
  - `_watch_for_stalls` hands silent workers to `incidents/stall.build(worker, …)`. That reads `<worker>.events` (beats and CPU) and `<worker>.state` (in-flight nodeid), and gets a stack.
  - `_live_pid(worker)` asks xdist's gateway for the pid.
- **Worker death** (`incidents/death.py` ~L320–L360, and ~L515–L530 for recovery) reads `<worker>.state` for `test_in_flight`/`last_test`/`phase`/counters.
- **Wire models** (`client.py`): `Worker` (L175) and the rest are `_Wire` models with `extra="ignore"`. **A field that isn't declared on `Worker` is dropped** by every consumer that parses with these models, and that includes the Sahara API.

## 6. Design

### Principle: a lane is a worker

Each lane gets **its own `.state` file, named after its lane id** (`ln3.state`, `gw0.ln3.state`), in the run directory beside the process's files. Everything that enumerates `*.state` or resolves names from state files (`/workers`, `/stack?worker=`, the UI's test→worker matching) then sees lanes as workers without being taught about them. Anything that is truly per-process (heartbeat, RSS, the pid, crash files) stays on the **process's** files, and a lane's state file names its process so readers can find them.

### 6.1 Plugin changes

Every change is conditional on "this hook is running on a lane". So a run without lanes follows exactly today's code paths and writes exactly today's bytes.

**C1 · A state slot per lane** (`capture/recorder.py`, `capture/state.py`)

- **The helper:** `_lane_of(item) -> str | None` returns `item.config.workerinput.get("workerid")` when that differs from `self.worker_id`, else `None`. It returns `None` when `workerinput` is absent (single-process main thread) or equals `gw0` (a hybrid worker without lanes, or its main thread).
- **The per-lane record:** keep a `dict[str, _LaneSlot]`, where `_LaneSlot` holds a `WorkerState`, `_counted` and `_attempt`, created lazily on the lane's first test. Create each `WorkerState` as `WorkerState(directory / f"{lane}.state", os.getpid(), run_id)` and extend its record with four optional keys:
  - `"process": self.worker_id` (`"main"` or `"gw0"`),
  - `"thread_name": threading.current_thread().name`,
  - `"thread_id": threading.get_native_id()`,
  - `"lane": True`.

  Add them as optional constructor arguments of `WorkerState`, written into `_record()` only when set. Existing records must stay byte-identical.
- **Routing:** every per-test mutation in `pytest_runtest_protocol` and `_phase()` goes to the lane's slot when `_lane_of(item)` is not `None`. That is the counters, `attempt`, the clocks, `timeout_settings`, `update(nodeid/phase/…)` and `_counted`/`_attempt`. Otherwise it goes to `self.state` exactly as today.
- **The process's own state:** the first time a lane slot is created, write `lanes=True` into the **process's** state (one more optional key), and leave its `nodeid`/`phase` alone from then on. That marker is how readers know the process row is a container, not a worker.
- **Thread safety:** two lanes never share a slot, since each lane is one thread. The shared dict is only inserted into under a `threading.Lock`. On free-threaded 3.14t, do **not** mutate `self.state` counters from lane threads.

**C2 · `/workers` shows lanes, and the process row goes away when it has lanes** (`topology.py`, `client.py`)

- In `topology.run()`, skip a state whose record has `lanes: true`: its lanes are the rows. Also skip it in `stack_server.worker_pid`'s name matching, so `?worker=gw0` still resolves. It still has the pid, so let it resolve as today, via its record's pid.
- In `topology.worker()`, a record with `"process"` set reads its heartbeat from `<process>.events`, not `<stem>.events`. `rss_mb`, `heartbeat_age_s` and `process_exists` are the process's; that's correct and expected, since memory is per process.
  - `cpu_rate` is per lane when C3 is in (§6.1 C3), else the process's.
  - Add to the row: `"process"`, `"thread_name"`, `"thread_id"`.
  - The schedule row (`rows.get(state.stem)`) is keyed by the xdist worker. For a lane, use `rows.get(record["process"])` only if the controller writes per-lane rows (it doesn't today). Otherwise leave `tests_assigned`/`queued` as `None`: "cannot say" is honest.
- In `client.Worker`, add `process: Optional[str] = None`, `thread_name: Optional[str] = None` and `thread_id: Optional[int] = None`. This is what lets the fields through the Sahara API.

**C3 · Per-lane CPU, so a hung lane in a busy process reads as blocked** (`capture/heartbeat.py`, `analysis/stall.py`, `topology._status`)

- The heartbeat's `_beat()` (~L103) today records `cpu_seconds=time.process_time()`. When lane slots exist, also record `threads={"<lane>": <cpu seconds of that native thread>}`. Get the figures from `psutil.Process().threads()`, which returns `(id, user_time, system_time)` with `id` the native thread id on Linux, macOS and Windows. psutil is already a required dependency. Match on the `thread_id` stored in each lane slot.
- Where the rate is computed for a row or a stall verdict (`stall_analysis.cpu_rate(beats)` in `topology.worker` and `stall.build`), compute it from `beat["threads"][lane]` when the row or worker is a lane and the key exists. Fall back to the process figure otherwise.
- **Why this matters:** the stall verdict "burning CPU → slow, not stuck" otherwise reads the *process's* CPU. That hides a blocked lane whenever any sibling is busy.

**C4 · Stall detection per lane** (`incidents/engine.py`, `incidents/stall.py`)

- In `IncidentEngine.pytest_runtest_logreport`, take `worker = getattr(report, "lane_id", None) or worker_of(node)`. When `report.lane_id` is present and the run records here (single-process), `_touch` the lane id, **not** `worker_of(node)` or `SOLE_WORKER`. That removes the false `ln0` `STALLED_SILENT` and the wrong-test blame.
- In `stall.build(worker, …)`, when `<worker>.state` has `"process"`:
  - read beats from `<process>.events`, using per-lane CPU from C3;
  - read the in-flight nodeid from the lane's state;
  - take the stack from the process, and name the lane's `thread_name` in the incident so the reader knows which thread to look at. Put the lane's thread first, or only the lane's thread, in `raw_stack` if that's simple.
- `_live_pid(worker)`: for `gw0.ln3`, ask for `gw0` (the part before the first `.`). In a single-process run it is this process's pid.
- The pytest-lanes thread name and lane id can also be recovered from the report (`report.lane_id`), so nothing else is needed from pytest-lanes.

**C5 · Worker death names each lane's test** (`incidents/death.py`)

- When a process worker `gw0` dies, also read every `*.state` whose `"process" == "gw0"` (glob `*.state` and filter; never build a path from a name).
- If exactly one lane was in flight, that is `test_in_flight`, with its phase and counters, as today.
- If several were, set `test_in_flight` to `None` rather than guessing. Add an optional `lanes_in_flight: list[{lane, nodeid, nodeid_hash, phase}]` so the report lists them all.
- **Without this, hybrid crash reports regress** to "no test running", because C1 leaves the process's own state idle.
- Do the same in the recovery path (~L515).

**C6 · Process-wide machinery that must not run per test under lanes** (`capture/recorder.py`, `capture/crash_stack.py`)

These all assume one test at a time per process. Under lanes they'd interfere with each other or report wrongly, so make each one lane-safe in the simplest way, and say so in an event (`events.record("lanes_adjusted", …)`):

- **Stderr tee (fd 2):** `_tee_take`/`_tee_hand_back` swap fd 2 per phase. Concurrent lanes would leave fd 2 pointing at the wrong file. pytest doesn't swap fd 2 per test under lanes (§4), so **take it once for the session and don't swap per phase** when lanes are active.
- **`SlowTestWatchdog`** (`crash_stack.py` L86): `start_test`/`end_test` run one clock. Either keep the clock per lane in the lane slot (preferred: it's a dict of lane → start time, and the watchdog dumps all threads anyway), or disable it under lanes with an event.
- **Profiler:** it attributes samples to one current test. **Disable it under lanes** (non-goal), with an event.
- **`heartbeat.nodeid`/`.phase`:** leave the process's beat without a nodeid under lanes. Per-lane figures are C3's `threads` map.

**C7 · Version:** bump `pyproject.toml` `version` **and** `src/pytest_failure_instrumentation/__init__.py` `__version__` from `0.13.1` to **`0.14.0`**. This is a minor bump: new optional fields and behaviour, no breaking change. Add a README section, "Running under pytest-lanes", covering the per-lane rows, the new fields, per-lane stalls, and what C6 turns off.

### 6.2 Sahara API

The mock and reference implementation is `Heknon/morphine-sahara-mock-api`. `ingest/live_view.py` says it is "written to be copied into the production API". It **imports and extends** the plugin's models (`WorkerRecord(Worker)` at ~L176), so C2's new fields pass through with **no code change**. The only change is the dependency: require `pytest-failure-instrumentation>=0.14.0`.

To verify, run `ingest/verify_live_view.py` against a run with lanes, and confirm that `process`, `thread_name` and `thread_id` reach the JSON. If production pins the plugin, bump the pin there too.

### 6.3 Sahara web UI (`Heknon/morphine-sahara-web`)

**Nothing is required for it not to break.** Lane rows are ordinary `WorkerRecord`s:
- `findWorkerForTest` (`pages/DashboardPage/CycleOverview/workerState/workerStatus.ts` ~L80) finds every running test.
- Rows are keyed by `server_id/worker` (`workerState/servers.ts` ~L78), which stays unique.
- Stacks are keyed by pid (`TestDetails/tabs/Worker/getCallstack.ts` ~L58), so lanes of one process share one read. That's correct.

The small change that makes it *right*:
1. **Types:** `src/types/worker.ts` `WorkerRecord` (~L137) gains `process?: string | null`, `thread_name?: string | null` and `thread_id?: number | null`, each documented.
2. **Carrying the thread:** `getCallstack.ts` `WorkerProcess` (~L12) and `workerProcess(record)` (~L28) carry `threadId: record.thread_id ?? null`. **Don't** add it to the query key; the stack is per process.
3. **Thread order:** in `tabs/Worker/threadOrder.ts`, `orderThreads(threads, pid, focusThreadId?)` puts the thread whose `os_thread_id === focusThreadId` (or `thread_name === record.thread_name`) first. Otherwise `MainThread` stays first, as today. `isMainThread` stays for runs without lanes.
4. **Wiring:** `CallstackThreads.tsx` (~L57–L59) already opens index 0, so ordering is enough. Pass the focus through from `CallstackSection` (`Worker.tsx` ~L184, `Workers/WorkerDetail.tsx` ~L54/L123). `callstackDiff.ts` (~L79–L89, lead thread) and `callstackText.ts` (~L51) should use the same focus.
5. **Tests:** add unit tests beside the existing `threadOrder.test.ts`, `callstackDiff.test.ts` and `workerStatus.test.ts`. A lane record with shared pid and `thread_id` must open its `lane-*` thread; a record without `thread_id` must behave exactly as today.

**Check, but don't necessarily change:** the Resources view joins processes to workers by name (`Workers/Resources/resourceReading.ts` ~L120–L140, `processOrigin.ts` ~L74–L77). Its processes are named after the xdist worker (`gw0`), while lane rows are `gw0.ln3`, so a lane row may find no process there. If so, match on `record.process ?? record.worker`. `processOrigin.ts` ~L151–L153 picks `MainThread` for process identity, which is right, because it is identifying the process.

### 6.4 Mock API (`Heknon/morphine-sahara-mock-api`)

Add **one lanes-shaped run** so the UI can be developed and screenshotted without a real run:
- In `src/data/cycles/workers.ts`, add a run with 2 processes × 3 lanes: worker names `gw0.ln0` … `gw1.ln2`, shared pids per process, `process: "gw0"`, `thread_name: "lane-gw0.ln1"` and `thread_id` set.
- In `src/data/cycles/callstack.ts`, add a stack for those pids with `MainThread` (the scheduler, in `_pump`) plus one `lane-*` thread per lane, each in its own test's frames, and `os_thread_id` equal to the lane's `thread_id`.
- Update the types in `src/types.ts` (`WorkerRecord` ~L674).

Keep all existing data unchanged, since runs without lanes remain the default.

### 6.5 pytest-lanes

No change is required. Add the end-to-end check in Appendix B to its contract tests, as a test that runs only when pytest-failure-instrumentation is installed. That turns lanes plus instrumentation into a regression test in all three modes.

## 7. Compatibility

| Run | Plugin output | Sahara API | Sahara UI |
|---|---|---|---|
| No lanes, new plugin | byte-identical to 0.13.1 | unchanged | unchanged |
| Lanes, old plugin (≤ 0.13.1) | today's degraded rows (§2) | unchanged | as today (degraded) |
| Lanes, new plugin, old API pin | lane rows; the new fields are dropped by the old models | works | lane rows work; stack opens on `MainThread` |
| Lanes, new plugin, new API | lane rows plus `process`/`thread_*` | passes fields through | lane rows, stack opens on the lane's thread |
| No failure instrumentation | — | existing `no_server` answer | existing "unavailable" state |

## 8. Implementation plan

Write the failing test first each time. **Run tests locally: GitHub runners are paid.** The plugin repo's `AGENTS.md` explains its CI policy; macOS runners in particular are expensive. Use `pytest -n 4` locally.

1. **Plugin, C1 + C2.** Tests (pytester, subprocess, needing `pytest-lanes` installed; skip if it isn't):
   - under `--lanes 3` and `-n 2 --lanes 3`, `/workers` (or `topology.snapshot()` directly) lists one row per lane, each with its own `nodeid`, and no process row;
   - `/stack?worker=gw0.ln1` resolves;
   - **without lanes, the state files and `/workers` JSON are byte-identical to a baseline captured from 0.13.1.**
2. **Plugin, C4 + C3:** the stall scenario in Appendix B must blame `test_s[envA-0]` in all three modes, with no `ln0` false alarm.
3. **Plugin, C5:** a hybrid crash with sibling lanes in flight lists them, and names the culprit when it is the only lane in flight.
4. **Plugin, C6:** under lanes, fd 2 is taken once, the watchdog is per lane or disabled, and the profiler is disabled. Each is recorded as an event.
5. **Plugin, C7:** version 0.14.0 in both places, and the README section. Run the full plugin suite locally without lanes; it must be green and unchanged.
6. **Mock API (§6.4),** then **UI (§6.3)**, with unit tests. Check in the browser against the mock's lanes run.
7. **API (§6.2):** bump the pin and run `verify_live_view.py`.
8. **pytest-lanes (§6.5):** the end-to-end contract test.

## 9. Open questions for the implementer to confirm, not decide alone

- Should `/workers` keep the process row, marked as a container, for a UI that later groups lanes? The proposal hides it, which is the minimal change.
- Per-lane `tests_assigned`: the xdist controller schedules to the process, not the lane, in hybrid mode, so this stays `None` for lanes. Is that acceptable in the UI's progress column? `workerProgress.ts` already handles `null`.
- Is disabling the profiler under lanes acceptable for now?

## Appendix A: repositories and commits read

| Repo | Commit | Notes |
|---|---|---|
| `Heknon/pytest-failure-instrumentation` | `9ecffa5` | version 0.13.1 |
| `Heknon/morphine-sahara-mock-api` | `a87697a` | `ingest/live_view.py`, `src/data/cycles/*` |
| `Heknon/morphine-sahara-web` | `ab160bb` | paths under `src/` |
| `Heknon/pytest-lanes` | branch `claude/pytest-contract-backlog-722ycm` | P10 lane identity, `report.lane_id`, `lane-<id>` thread names |

## Appendix B: the end-to-end scenarios

Both use this conftest (the user's scheduling pattern, with one environment per lane):

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

**Live view.** Six environments × 2 steps, each test sleeping 4s inside a function named after its environment (`work_envA` …). While it runs, `GET /workers`, `GET /stack?worker=<each>`, `GET /stack?pid=<each>` and `GET /stack?worker=gw0.ln0`. Run with:

```
pytest -p no:warnings -p no:cacheprovider <mode> --failure-instrumentation --callstack-port 18765 -o failure_directory=evidence
```

Expected after this work: one row per lane with its own `nodeid`, and `?worker=gw0.ln0` resolves.

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

- Today, `-n 2` gives `worker_stall gw0 … test_s[envA-0] … test_stall.py:6`, which is correct. `--lanes 3` gives the wrong test plus `ln0 STALLED_SILENT`, and `-n 2 --lanes 3` gives "no test running … in _pump".
- Expected after this work: all three modes blame `test_s[envA-0]` at line 6, with no other stall incidents.

**Death (hybrid).** Three environments × 2 steps. `envB` step 0 calls `os._exit(1)` once, using a flag file under `tmp_path_factory.getbasetemp().parent`. Run with `-n 1 --lanes 3`. Expected: the death incident lists the three lanes in flight (§6.1 C5), and xdist replaces the worker.

## Appendix C: cross-check with `--lanes-detect`

pytest-lanes' shared-state detector (`pytest --lanes-detect`, added after this doc was first written) was run on a small suite with `--failure-instrumentation` active. With no hints, it reported this per-test state in the plugin's objects. Every item is already covered by §6.1:

| Reported path | What it is | Covered by |
|---|---|---|
| `plugin:failure-instrumentation-recorder._counted`, `._attempt` | per-test bookkeeping in `WorkerRecorder` | C1 (lane slot) |
| `…-recorder._open_resources[0].nodeid` / `.phase_started` / `.attempt` / `.tests_started` / `.tests_finished` / `.test_started` / `.sequence` / `.last_nodeid*` / `._hashed` | the process's `WorkerState` record (reached through `_open_resources`) | C1 (a `WorkerState` per lane) |
| `…-recorder.heartbeat._identity` | the heartbeat's single nodeid/phase | C6 (no nodeid on the process beat) and C3 |
| `…-recorder.heartbeat.tickers[0]._started_at` | `SlowTestWatchdog`, one clock per process | C6 (per-lane clock, or disabled) |
| `plugin:failure-instrumentation-controller.activity['main']` | stall detection keyed by `SOLE_WORKER` | C4 (key by `report.lane_id`) |

**How to use the detector here:** after C1–C6, a sequential run still reports the process-level state (without lanes the plugin writes the process's own slot, exactly as today), so the detector is not the acceptance test for this work; the three-mode end-to-end scenarios in Appendix B are. It is useful as a regression check: any *new* per-test state in the plugin shows up in its report. Run it as `pytest --lanes-detect --lanes-detect-report=shared.json --failure-instrumentation`, and compare `findings` with this table.
