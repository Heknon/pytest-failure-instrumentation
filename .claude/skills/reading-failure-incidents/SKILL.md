---
name: reading-failure-incidents
description: Read and triage an incident raised by pytest-failure-instrumentation - the enriched alerts for pytest failures that happen outside the call phase (worker death, worker stall, collection mismatch, internal error, run summary, stack server unavailable) and the profiler's findings (cpu hotspot, cpu burst, memory profile). Use when an alert whose first line ends with a [worker_death ...], [worker_stall ...], [collection_mismatch ...], [internal_error ...], [run_summary ...], [stack_server_unavailable ...], [cpu_hotspot ...], [cpu_burst ...] or [memory_profile ...] tag appears in CI output or a bug report, when a stored incident payload or pytest_failure_incident hook argument needs interpreting, or when asked what a verdict, owner, severity, confidence or fingerprint field means. Not for ordinary assertion failures, which explain themselves.
---

# Reading a failure incident

An incident is one structured record per problem, raised by
pytest-failure-instrumentation through the `pytest_failure_incident` hook. It
comes in two forms carrying the same facts:

- **The alert text** — `str(incident)`. What lands in Slack, a log or a bug
  report. Indented block, headline first.
- **The payload** — a pydantic model, one class per `kind`. What a database
  stores. Parse a stored row with
  `pytest_failure_instrumentation.incidents.registry.parse(row)`;
  `registry.json_schema()` is the contract for the whole union.

Everything the alert prints is in the payload; the reverse is not true, because
the text is trimmed for a reader. **For anything quantitative — counts, id
lists, per-worker samples — read the payload, not the text**, and where you
only have the text, say a number is what was *shown* rather than what there is.
Nothing licenses extrapolating past what the incident actually compared: two
variants recorded means two were compared, however many workers were running.

Whoever is reading this usually has the alert and *not* the plugin's source, so
this file carries the facts you would otherwise have to read `classify.py` and
`severity.py` to know. They are what separate reporting the finding from
inventing one.

## Anatomy

Every kind renders to one shape:

```
Worker gw1 crashed with SIGSEGV (segmentation fault in native code) while running test_crashes.py::test_crashes (call), in native_call (engine.py:6)   [worker_death NATIVE_CRASH, product, critical]
    Exit status -11 read via waitid (pid 805).
    The worker wrote a stack as it died.
    Look at: test_crashes.py::test_crashes.
    Measured: 1 test started and 0 finished on this worker.
```

| Line | Is |
|---|---|
| the first line | what happened, in words, specific to this instance: which worker (the xdist gateway id that matches the `[gwN] node down` line in pytest's own output, `main` for a run with no workers), which signal, which test, which frame (`blamed_frame`, the first frame on the stack owned by somebody). It ends with the tag `[kind VERDICT, owner, severity]`, with `run-ending` appended when the session died with it; a `run_summary` has no owner slot, because nothing failed |
| `No stack was captured; the owner is taken from …` | `suspect_owner` and `suspect_basis` — a lead, not a finding; printed only when there was no stack |
| every other line | exactly one of three things: a **measurement** (what was observed, and where the figure came from), what it **means by construction** (what follows from how it was measured, or from a fact about the OS, the runtime or xdist), or a **place to look** (`Look at:` a file, line, test, setting or flag). Nothing guesses at a cause in the user's code, and nothing prescribes a fix to it. Several numbers share one `Measured:` line at the end |
| indented sub-rows | a table: the diff of a collection, the parameter values each worker produced |

The lines under the first are the `evidence` field, plus a few a kind derives
from its own fields at render time (the variant rows of a collection
mismatch, where a stall's stack came from). The convention is held by
`tests/test_message_convention.py`, and its rationale is in
`incidents/base.py`.

## The numbers that mislead

Every field here reads as something it is not, and each one has cost somebody a
wrong conclusion. Check this list before quoting a figure back to anyone.

| Field | Reads as | Actually is |
|---|---|---|
| `silent_for_seconds` | how long the worker hung | the silence measured when the poll noticed it: at least `failure_stall_seconds` (default 300) and up to one poll interval more. The hang continued after this until something killed the job — the incident is raised *at detection*, not at the end. Close to the configured threshold, but never read that setting back out of it |
| `rss_mb_at_death` | memory at the moment of death | the last heartbeat sample, up to one interval stale (default 5 s). A worker that ballooned inside one window still prints the old figure |
| `· SIGKILL with no cgroup OOM event` | the counter was read and was flat | identical text whether it read zero **or could not be read at all**. `capabilities.cgroup_oom_counter` is the tiebreak: `true` means genuinely flat, `false` means unmeasurable, and OOM stays open |
| a memory line with no `of a N MB cgroup limit` | no limit is being hit | no limit was *discovered*. These workers may not be memory-limited at all, in which case raising a container limit changes nothing |
| `cpu_rate` | raw CPU seconds | cores' worth across the sampled window; under 0.05 counts as no progress. `null` means unmeasurable, which is not the same as zero |
| `severity=critical` | urgency, blast radius | ownership routing. It says the blamed frame is in a package the project declared as its own — the same deadlock in a test file would be `informational` |
| `suspect_owner` | who did it | who *might* have; set only when no stack named anybody |
| `started=N finished=M` | throughput | where in the worker's life it died. `started=1 finished=0` is a death on the very first test, not a leak accumulating over a long worker lifetime |
| `missing` / `extra` | the whole difference | capped at 500 per side. `missing_count` / `extra_count` are the true totals |
| `test_in_flight` | the node id, verbatim | written to a fixed-size slot, so a very long id is elided from the middle and marked `...` — head and tail are kept, since the module is at the front and a parametrized hash at the end. Match on the parts, not the whole string, and do not report an elided id as the test's real name |
| `last_test` | the test that failed | the last test the worker *ran*, set whether or not it finished. It is only ever context. When `test_in_flight` is null nothing was running — the worker was between tests, still collecting, or waiting to be handed work — and `last_test` had already finished. Never report it as the test that died or hung |
| `test_in_flight: null` on a stall | the worker had no work | one of three ordinary things that look identical from outside: between tests, collecting, or idle awaiting work. The silence is still real (the run cannot end while a worker never comes back) but the confidence drops to `low` and nothing is blamed on a test |
| `no stack: pid … could not be confirmed` | the probe failed | the probe was *never sent*. `SIGUSR1` terminates by default, and the pid came out of a file — an exited worker leaves its number to be reused, so an unconfirmable pid is left alone rather than signalled. Not a finding about the worker |
| `exitstatus` on a `run_summary` | the run's outcome | sometimes reported before pytest applies `INTERNAL_ERROR`; `run_ending_incidents` is the one to trust when they disagree |
| `run_ending_incidents` on a `run_summary` | that many incidents ended the run | that many were *raised as* run-ending. A summary exists only because the run reached session finish, so a stall counted here is one that resolved — the worker came back. For an `internal_error` the count is the correction to a `0` exit status; for a `worker_stall` it is a prediction the summary itself disproves |

## The shared fields

**`owner`** — `product`, `third-party`, `customer-code`, `runtime`, `unknown`.
Whose code is on the stack, found by walking outward past runtime frames to the
first frame belonging to someone. `runtime` is a positive finding — no test code
anywhere on the stack, so the framework itself is what failed — not a missing
answer. Only `unknown` means nothing was determined.

The reverse reading matters just as much: `suspect_owner: null` together with a
`blamed_frame` means no guessing happened at all, so `owner` is a finding you
can state flatly.

**`severity`** — derived from `owner`: `product`→critical, `third-party`→high,
`customer-code`/`runtime`→informational, `unknown`→needs-triage. Three
overrides are informational: a `run_summary`; a `SIGNAL_*` identified with high
confidence; and a run somebody deliberately stopped - `KILLED_BY_PROCESS`,
`KILLED_AFTER_SIGTERM`, `RUN_STOPPED` - where the same list also stops any test
being suspected, since a cancellation is nobody's defect. A framework defect
that ended the run is raised the other way, to high, because no test is at
fault and nothing else will ever surface it. `needs-triage` means "somebody has
to look", not "this is bad".

So `severity` answers *who acts*, and `run_ending` is the closest thing to a
blast-radius field: it says the session had no path to completion. Anyone
routing these — deciding what pages and what becomes a ticket — wants the
second field, not the first.

**`confidence`** — `high`, `medium`, `low`, about the *verdict*. A medium
verdict is a shortlist, not a cause; say so rather than overselling it.

**`fingerprint`** — stable across runs, excluding worker id, pid, timings and
memory, so one defect on twelve workers is one incident with a count. It is the
cheapest question anyone can ask of an incident: has this fired before? First
occurrence and long-running quiet recurrence call for different responses.
Duplicates are collapsed within a single run only, so grouping across runs is
the reader's job, and this is the key to do it on.

**`capabilities`** — what the machine could measure. Check it before concluding
anything from an absent figure: a missing memory number means unmeasurable
there, not healthy.

**`INSTRUMENTATION_FAILED`** as a verdict means gathering the incident raised.
The underlying failure was real; only the detail is missing.

## Per kind

### `worker_death` — the process ended when it should not have

xdist's own report is `node down: Not properly terminated`. The verdict is what
replaces it:

| Verdict | Means | Points at |
|---|---|---|
| `OOM_KILLED` | `-9` **and** the kernel log names this pid as the victim, within its death window during this run | memory. Read `oom.pressure`: `fleet` means the run's processes exceeded the limit together (fewer workers or more memory); `own weight` means this one was far above its peers (the test in flight) |
| `KILLED_BY_PROCESS` | `-9`, and the kernel's signal tracepoint saw a process *outside the run* send it | nobody's code. `killer` names the sender - a CI runner cancelling, a timeout enforcer, a hand on the keyboard. Informational, and no test is suspected |
| `KILLED_BY_RUN` | `-9` from another process of this run | the controller (execnet terminating a worker that did not exit in time) or a sibling worker - a test that kills processes |
| `SELF_KILLED` | `-9` the worker sent to itself | the test in flight: an `os.kill(os.getpid(), SIGKILL)` or a library doing the same |
| `POSSIBLE_TIMEOUT` | a self-exit (code 1) or SIGALRM that reached its effective terminating timeout; medium-confidence correlation, not proof | the hung test in flight - `matched_timeout` and `timeout_source` name the limit and enforcer, `test_seconds` how long it ran. Under heavy xdist parallelism a starved-slow test hitting the timeout is the common one |
| `KILLED_BY_KERNEL` | `-9` with `si_code` `SI_KERNEL`, and no readable OOM log | the OOM killer, most likely; `kill_sources.kernel_log` says why the log could not be read |
| `KILLED_AFTER_SIGTERM` | `-9`, and the controller had been sent SIGTERM shortly before | the run was being stopped; `signals_before_death` names who asked. Informational, no test suspected |
| `RUN_STOPPED` | a recovered run whose controller was sent SIGTERM before this process's last heartbeat | the same - the process ended with the run rather than on its own |
| `SIGKILLED` | `-9` and no witness answered | something outside the container: host-level OOM, CI/container cancellation, runner preemption, an external kill. The `kill witnesses:` evidence line says which source was withheld and why - that is what to fix before guessing |
| `NATIVE_CRASH` | fatal signal, or a Windows NTSTATUS | the blamed frame — a C extension or a ctypes call |
| `SIGNAL_<n>` | SIGTERM/SIGINT/SIGHUP | nothing, unless the run was not meant to be stopped |
| `SELF_EXIT` | an exit status and no signal — **including 0** | `sys.exit()`, `os._exit()`, or a plugin aborting. A worker that left without being asked to has gone wrong whatever number it exited with, so a clean 0 here is a finding, not an all-clear |
| `PROBABLY_SIGNALLED` | exit code 128–191 | a wrapper that ate the signal; the true one did not survive |
| `UNKNOWN` | no status obtainable — a remote gateway, or a run recovered after the fact | nothing — do not guess one |

`-9` alone never licenses "we ran out of memory"; only a witness does - the
kernel log naming the pid, or the cgroup counter moving while
`capabilities.cgroup_oom_counter` says it was readable. That distinction is
the reason the verdicts above exist rather than one.

**`recent_output`** is the tail of what the worker printed, when
`failure_capture_output` was on: the native line a crash leaves and no stack
carries - `OpenBLAS ... pthread_create failed`, a malloc abort. It reads fd 2
directly into a file, so it keeps even the line printed in the phase that
crashed, which pytest's own capture never reports. Empty means it was not
captured (the setting was off) or the worker was silent - the `last stderr:`
evidence line is present only when there was something to show.

**`test_seconds`, `matched_timeout`, `timeout_source`** time the death against
the run's timeouts - see `POSSIBLE_TIMEOUT` above. `test_seconds` is on every death
with a test in flight, so a `SELF_EXIT` can be seen to be near a limit even
when it did not quite reach one.

**`killer`, `oom`, `signals_before_death` and `kill_sources`** are the
witnesses. `killer` is the signal that ended the process with its sender's
pid, uid, comm, command line and `sender_role` (`itself`, `this run's
controller`, `gw3, another process of this run`, `outside this run`). `oom`
is the kernel's own record: the constraint (`CONSTRAINT_MEMCG` is a cgroup's
limit, `CONSTRAINT_NONE` the machine's), the cgroup, and the table it weighed
- `run_tasks`/`run_rss_mb` are this run's share of it, `victim_rank` where the
victim stood, and `triggered_by_*` the process whose allocation hit the limit,
which is often not the victim. `signals_before_death` is what was sent to this
worker or to the controller in the minutes before, above all a SIGTERM.
`kill_sources` says what each witness could do on this machine; a `SIGKILLED`
or `UNKNOWN` verdict is only as unknown as that record says, and the remedy is
usually there (`failure_elevate` on a runner with sudo; a readable
`/dev/kmsg`; administrator rights on Windows). On Windows `killer.name` is
`TerminateProcess` rather than a signal, `killer.signal` is 0, and
`killer.exit_code` is the code the caller passed - `1` is `taskkill /F` or a
Go program such as the GitLab runner, `-1` (4294967295) is .NET's
`Process.Kill`, `15` is Python's `os.kill`.

Two absences carry information here. No `of a N MB cgroup limit` clause means
no container limit was discovered, so raising one may change nothing. And no
`system had N MB free` line means no high-water snapshot ever fired — the
worker never came near a ceiling — which is evidence against memory in its own
right.

**`worker=controller` with `recovered_from_run` is a cancelled or killed run.**
The controller runs no tests and keeps no heartbeat, so it is recovered from
its marker and its own log rather than from an event log: a job cancellation
is a controller sent SIGTERM while its workers finish cleanly on execnet's
SIGINT, and this is the one incident such a run produces. `killer` names who
sent it; the verdict is `SIGNAL_15` when a witness saw the signal and
`UNKNOWN` when none did, with `kill_sources` saying why.

An incident like that may also arrive through `failure_on_run_death` rather
than the hook: the sidecar that outlived the killed run reports it at once,
with the same fields, and stamps the run's marker `reported_at` so no later
run raises it again. `raised_at` is then within a minute of the death rather
than whenever the next run happened.

**`recovered_from_run` means this is about a different run from the one that
reported it.** A run whose own process was killed has no survivor to report it,
so the next run over the same evidence directory does. When that field is set:
the alert's first detail line names the dead run, `run_id` is *that* run's id
rather than the reporting run's, and `raised_at` is when it was found rather
than when it happened — `last_seen_at` is the dead run's final heartbeat, and
the death is somewhere between the two. Nothing here says anything about the
run that reported it, which is the misreading to head off: it did not crash.

Such an incident has no exit status and cannot have one. Only a parent may read
it, the parent was the run that died, and the process was gone before anything
looked — so `UNKNOWN` here is a fact about who was entitled to ask, not a
capability of the machine, and no setting recovers it. A fatal stack still can
be kept (`failure_crash_stack`), and where one was, the verdict reaches
`NATIVE_CRASH` with a blamed frame on the strength of the dump alone. Where one
was not, the incident says where it went instead of leaving the absence to be
read as "it left nothing behind".

### `worker_stall` — alive, but stopped reporting

Silence proves nothing on its own: the controller hears from a worker only when
a phase completes, so a twenty-minute test and a deadlock look identical from
outside. The heartbeat's CPU time is what separates them.

| Verdict | Heartbeat | CPU | Means |
|---|---|---|---|
| `STALLED_BLOCKED` | alive | none | the test thread is waiting on something that is not coming |
| `STALLED_FROZEN` | stopped | — | native code holding the GIL, or the process stopped |
| `STALLED_SILENT` | never ran | — | the watchdog is off, so there is no passive evidence either way — `confidence` is low for a reason |

A merely slow test — alive and burning CPU — is never reported at all, which is
why a stall that *is* reported is not "the suite got slow".

`stack_source` says which mechanism left the frames, and they are not
interchangeable. `probe` and `py-spy` are taken at detection and describe now;
`watchdog` and `frozen-fallback` were written earlier and unprompted, and the
alert prints how old they are for that reason. `py-spy` is a run with no
workers being read from outside — no signal was sent, so nothing could have
perturbed the stall being measured, and it is the one way a current stack
exists on Windows; its absence there means py-spy could not run or could not
read the process rather than anything about the run. `frozen-fallback` means more than a stack: the
interpreter had stopped running Python at all.

Two things follow from `run_ending` being true here. The run has no path to
completion, because xdist waits for work it handed out and never gets back — so
a job that hangs to its timeout is the symptom and the stall is the cause. And
the incident was raised at the threshold, often an hour before that timeout, so
the useful advice is usually to act on the incident rather than wait.

That is an inference from the evidence at detection time, not an observation,
and one thing can falsify it: a `run_summary` arriving afterwards. The summary
is written at session finish, so its existence proves the run got there — the
wedged worker came back. Treat a stall followed by a summary as a hang that
resolved, worth fixing and not worth paging about; a stall with no summary
beside it is the run-ending one.

`stack` is asked for *after* the verdict is reached, because asking a wedged
process a question can change its answer. `stack_probed: false` means the
platform was never asked (Windows, or probing disabled) — a fact about the
machine, not about the worker.

### `collection_mismatch` — workers disagree about which tests exist

Read as: **how many distinct opinions existed, who held each, and how the
minority differs from the majority.** Rows follow `variant_count`, not worker
count; `role="baseline"` is the largest group and everything else is measured
against it.

| Verdict | Means |
|---|---|
| `COLLECTION_MEMBERSHIP_DIFFERS` | a test exists on one machine and not another |
| `COLLECTION_ORDER_DIFFERS` | same tests, different sequence — fatal too, since xdist addresses tests by position |
| `COLLECTION_PARAMETERS_UNSTABLE` | same tests, different parameter values — a parametrize evaluated at collection time (`random`, a timestamp, an unordered set, a live call) |

`COLLECTION_PARAMETERS_UNSTABLE` is the one people misread, because the symptom
xdist prints — "Different tests were collected between gw0 and gw4" — sounds
like tests going missing. Nothing is missing: the plugin reached that verdict by
stripping the parameters from the ids and finding the collections identical. Two
further things a reader usually wants:

- **It is not worker-specific.** Every worker generates its own values; the pair
  named in xdist's message is whichever two it compared first. Nothing is
  special about gw4.
- **A single-process run cannot reproduce it**, because one collection cannot
  disagree with itself. Two `pytest --collect-only` runs diffed against each
  other can, without xdist at all.

The values in `parameter_samples` are the diagnosis rather than the location:
disjoint ids mean something live is being called at collection time, floating
point noise means a random draw. And warn about the tempting non-fix — pinning
`ids=` while leaving the draw alone silences the mismatch and leaves every
worker running *different data under identical names*, which fails in a way
nothing can reproduce. Make the values deterministic, or move them out of
collection into a fixture.

A variant with `compared=False` was never diffed — full id lists are kept for
the first five variants only. Do not describe it as agreeing, or as reordered.

`run_ending` is not constant for this kind: xdist aborts when the *initial*
collections disagree, and silently drops a late replacement worker instead. The
field says which happened.

### `internal_error` — pytest raised inside its own machinery

Always run-ending, and pytest fires no terminal summary for it, so nothing else
reports it. Check `first_hand`: `false` means this is xdist's re-raise of a
worker's error, so the traceback names xdist's frame rather than the failure and
worker attribution is unreliable. `exception` is the real `SomeError: message`
line; `detail` is the traceback, tail-truncated. `owner=runtime` plus run-ending
is the case severity raises to high.

### `run_summary` — one per run, whose *absence* is the finding

`verdict=RUN_FINISHED`, always informational, emitted for single-process runs
too. It says the reporting process reached the end — **so a run with no summary
is a run whose controller died**, which nothing inside that process could tell
you. Worth checking alongside any other incident: its absence turns "one worker
died" into "the whole job was killed". `incidents` maps fingerprint → count.

### `stack_server_unavailable` — the live view was asked for and is not there

The only kind that reports on the instrumentation rather than on the run. It is
raised when `failure_stack_server` was switched on and the server could not
serve, because otherwise nothing says so: the run is unaffected, and a UI with
no data looks exactly like a machine with no tests running.

`PORT_TAKEN` means something that is not one of ours holds the port — name
another with `--callstack-port`. `BIND_REFUSED` means the address could not be
bound at all, which naming another port does not fix. `requested_port` is what
was asked for and `drawn` says whether a port was to be drawn (`0`) or named.

Always `owner=runtime` and `severity=informational`: nobody's test is at fault
and nothing is broken. **It is never raised because another pytest session holds
the port** — that is the shared mode working as designed, so seeing this kind
always means a stranger or a bad address, never a colleague.

### How the profiler's findings are printed

The same shape as every other kind (see Anatomy), with the severity always
`informational` because nothing failed. The location is in the first line
for a CPU finding and in the first evidence line for a memory one.

```
CPU hotspot: load_everything (loader.py:14) used 21% of this run's CPU, 5.2 s   [cpu_hotspot PYTHON_CODE, product, informational]
    The time is in this function's own lines, not in calls it makes. Mostly line 14 (100%).
    Seen in 2 tests: tests/test_loading.py::test_loads_the_export, tests/test_index.py::test_index_is_complete.
    Look at: loader.py:14
```

### `cpu_hotspot` — a function that burnt a share of the run's CPU

Raised only when profiling is on (`--failure-profile` or `failure_profile`), and a
finding rather than a failure: nothing broke, and it is `severity=informational`
whoever owns it. `owner` is still worth reading — it says whether the function
is yours to fix, a dependency's, or the customer's own test code.

The numbers: `share_percent` is the function's share of every CPU second the
samplers attributed over the whole run, `cpu_seconds` the same in seconds, and
`test_count` how many tests it was seen in, with the three it cost most in
under `tests`. The profile is weighted by CPU, not wall time: a thread waiting
on a socket contributes nothing, so a large share means cores burnt, never
time waited.

The verdict says what kind of cost this is — it does not say the function is
wrong:

- `PYTHON_CODE`: the function's own lines are hot (`self_share_percent` near
  100), with `hottest_lines` naming them. A per-pixel loop, a hand-rolled
  parser: the shape a vectorised or native call replaces.
- `LIBRARY_CALL`: the cost is under a call it makes, and `below` names the
  library and function. The fix is in how it calls, or what it calls with.
- `BACKGROUND_THREAD`: the CPU is on `thread`, which is not the thread running
  the test. Paid whatever test is in flight, so a per-test view never shows it
  — this is the usual answer to "why is this worker at 30% between tests".
- `GC_PRESSURE`: the collector, which belongs to no frame; `tests` names the
  tests that drove it by allocating. No `blamed_frame`, `owner=runtime`.
- `NATIVE_THREADS`: CPU in threads Python has no stack for, named by their
  kernel thread names in the evidence. The answer is outside the interpreter.

`blamed_frame` is the first frame on the stack that belongs to somebody, walking
out from the innermost — so a C accessor called two million times is charged
to the function that called it, and `raw_stack()` holds the rest of that stack.

### `cpu_burst` — a stretch of the run where a core was held

Also profiling-only and informational, and the complement of `cpu_hotspot`:
a share of the run's CPU says nothing about a suite that waits on I/O for
ninety-nine seconds in a hundred and pins a core for the hundredth. The
profiler keeps a timeline — the process's CPU every tenth of a second against
the machine's — and a burst is a run of windows at or over
`failure_profile_burst_cores` cores. `burst_seconds` is how long it held them,
`cores` how many, `started_s` how far into the test (or the gap between
tests) it began, `phase` which phase, and `machine_busy_percent` how busy the
whole machine was meanwhile. The stack is the one that was there for most of
the burst, blamed like a hotspot.

- `LONG_BURST`: `nodeid` held a core for `failure_profile_burst_seconds` or
  longer. The evidence says how much of the test's CPU is in this one stretch
  and how much of its duration was waiting — a test that is 96% one burst is
  an I/O test with a CPU step in it, and the step is the line to open.
- `RECURRING_BURST`: the same function burst in `test_count` tests (five or
  more), whatever the length of each; `cpu_seconds` is the total, and
  `burst_seconds` the typical one. In `setup` it is a fixture doing the same
  work for every test that asks; in `call` a helper doing per call what it
  could do once. One finding for all of them, fingerprinted on the frame.
- `BACKGROUND_BURST`: `thread` is not the one running the test — between tests
  when `nodeid` is null, under a test otherwise. A poller, a log shipper, a
  client's keepalive: paid whatever test is in flight.
- `CONTENDED`: the machine was over 90% busy for `machine_busy_percent` of
  the sampled time and a worker got `cores` of a core while it was; `workers`
  and `cpus` say whether this run did it to itself. No frame, `owner=runtime`:
  every test's duration in such a run includes queueing for a core, and none
  of them is slower for a reason on its own stack.

A pinned machine is also noted on a `LONG_BURST` — "this got a slice of a
core and took longer than its CPU cost" — because a two-second burst at a
third of a core is six hundred milliseconds of work, not two seconds of it.

### `memory_profile` — a test that changed the worker's memory

Also profiling-only and informational. `nodeid` is the test, `before_mb`,
`after_mb` and `peak_mb` are resident memory around it, and `delta_mb` is what
the verdict measures. There is no stack: a resident-memory step is a fact about
a test, so `suspect_owner` is the owner of the test's module — unless the
sampler saw the climb, or allocation tracing was on, in which case there is
one; see below.

- `RETAINED_AFTER_TEST`: the worker was left `delta_mb` bigger, and the memory
  is still in use — the evidence carries the live-heap readings that say so.
  `phase` says where it arrived: `setup` is a fixture, and a session or module
  fixture keeps it for the rest of the run by design; `call` is the test's own
  body, which means a cache, a module-level list, or a leak.
- `HEAP_NOT_RETURNED`: the worker was left bigger but none of it is in use.
  The objects were freed and the allocator kept the pages mapped. Not a leak;
  it costs the worker's footprint, and hunting the code will find nothing.
- `TRANSIENT_PEAK`: climbed `delta_mb` and came back. Costs peak memory, which
  is what decides how many workers fit on a machine.
- `STEADY_GROWTH`: `worker` drifted up by `delta_mb` over `growth.tests`
  tests, `growth.per_test_mb` each (and `growth.objects_per_test` live
  objects, where the count was read), none of them enough to be raised alone
  and no single step half of the total. Two megabytes a test is what a leak
  looks like from outside, and the one thing a per-test check never sees;
  `nodeid` is only the first of them. The evidence says when every one is a
  parametrisation of the same test, and — with `--failure-profile-allocations` —
  which lines held what the worker accumulated; without it, it says to rerun
  with that flag.
- `WORKER_IMBALANCE`: `worker` peaked at `peak_mb` against a median of
  `median_mb` among its siblings (`worker_rss` has all of them), and `nodeid`
  is the test after which it first stood clear. Under xdist the worker that
  happened to receive the heavy fixture is the one that holds it.
- `PEAK_OVER_CEILING`: the test climbed to `peak_mb`, at or over the configured
  ceiling, whatever it started from and whether or not it came back. The size
  is the finding: a worker that reaches it once needs the machine to have it.
- `ALLOCATOR_RETENTION`: `worker` grew over its whole run and nothing is using
  the growth — `delta_mb` is memory the C allocator was handed back and kept
  mapped, `allocator_free_mb` how much it holds that way at the end. No
  `nodeid`, no stack, `owner=runtime`: no test did it and a leak hunt finds
  nothing. The evidence says which of two causes it is, and they have
  different fixes. `arenas` above a handful for `threads` threads with most of
  the free memory in the thread arenas is glibc giving every allocating thread
  its own arena: set `MALLOC_ARENA_MAX=2` in the workers' environment. Free
  memory mostly in the main arena is one fragmented heap, and the evidence
  gives `trim_mb`, what `malloc_trim(0)` would return now; the arena variable
  does nothing for that one. `worker_rss` lists every worker it was seen on.

For the verdicts about one test's climb — `RETAINED_AFTER_TEST`,
`TRANSIENT_PEAK`, `PEAK_OVER_CEILING` and `HEAP_NOT_RETURNED` — `blamed_frame`
and `raw_stack()` are the code that was running while the memory climbed,
with `climb_mb` of the `climb_total_mb` seen charged to it, and `owner` is
attributed from that stack as for a crash. That is the line to open: a loader
that reads whole rather than streams, a fixture that builds the world. When no
climb was seen — a step too fast for the sampler's resident-memory reading,
which is every second tick, 40 ms apart at the default interval,
or a worker with no stack to read — there is no frame and `suspect_owner` is
the owner of the test's module instead.

With `--failure-profile-allocations` the evidence also carries what tracemalloc saw:
"Held at the peak: 57.2 MB allocated at loader.py:12, called from reports.py:40"
(allocation site first) and "Still held after the test: …" for what the test kept. These name
the *holder* where the sampled stack names the *runner*, and they differ
exactly when a leak is one function's result kept by another. The figures are
then tracemalloc's own — Python allocations only, the tracer's tables left
out — and the evidence says so, with the resident figures beside them. A
`?` in a frame line is a tracemalloc frame, which carries no function name.
A traced run raises no `cpu_hotspot` or `cpu_burst` at all, because the
tracer's cost is in every CPU figure; their absence from such a run means
nothing.

## The decision each kind forces

Every one of these reaches a person who has to do something before morning.
The incident usually settles it, and saying so is most of the value.

| Kind | Already true when you read it | What it argues for |
|---|---|---|
| `worker_death` | xdist started a replacement and the run went on — unless `recovered_from_run` is set, in which case the run it describes is long over and this one is unaffected | fix or route by `owner`; a retry is reasonable only when the cause was external (`SIGNAL_*`, a cancellation) |
| `worker_stall` | the run has no path to completion and is burning runner time until a timeout | kill the job now rather than waiting it out. A `STALLED_BLOCKED` deadlock is deterministic, so a retry hangs the same way — the incident is worth more than the rerun, and it already holds the live stack you cannot get afterwards |
| `collection_mismatch` | the run aborted, or quietly lost a worker | retrying repeats it — an unstable parametrize re-rolls its values, an environment-dependent collection diverges the same way again. Nothing improves until collection is deterministic |
| `internal_error` | the session is over | nobody's test is at fault, so no test-level triage will surface it — it needs the framework or plugin owner |
| `run_summary` | the run ended cleanly | nothing, except as the control: its absence next to another incident means the controller died too |
| `stack_server_unavailable` | the run finished normally and nobody could watch it live | reconfigure rather than retry — the same port will be taken again. Nothing about the tests is in question, so it argues for a settings change and never for a rerun |
| `cpu_hotspot` | the run finished; this is where its CPU went | a look, not a rerun. Route by `owner`; a `BACKGROUND_THREAD` or `NATIVE_THREADS` verdict is the answer to a worker sitting at a steady percentage with nothing to blame |
| `cpu_burst` | the run finished; this is where its CPU went *in time* | `RECURRING_BURST` in `setup` is the fixture to make session-scoped or cache; `LONG_BURST` is one test's step to open; `BACKGROUND_BURST` is the thread to find; `CONTENDED` argues for fewer workers, not for touching any test |
| `memory_profile` | the run finished; this is what a test did to the worker's memory | `STEADY_GROWTH` and `RETAINED_AFTER_TEST` in `call` are worth a leak hunt, and `--failure-profile-allocations` on the same tests is the next run; `HEAP_NOT_RETURNED` and `TRANSIENT_PEAK` are sizing questions, not code defects; `WORKER_IMBALANCE` argues for isolating the heavy module; `ALLOCATOR_RETENTION` is an environment change — `MALLOC_ARENA_MAX=2` or a `malloc_trim`, the evidence says which — and never a test |

`run_ending` is the field to automate on. An incident raised at detection can
beat a CI timeout by the better part of an hour, and that lead time is only
worth something if something acts on it.

## Settling it on the next run

When the evidence runs out, these change what the *next* failure can tell you.
Recommending one beats speculating about the last one.

| Setting | Turns |
|---|---|
| `failure_memory_limit_mb` | an uncatchable `-9` into a `MemoryError` with a traceback and a node id (costs a hard per-worker ceiling) |
| `failure_tracemalloc_depth = 1` | an OOM into the source line holding the memory |
| `failure_heartbeat_interval` | a coarse memory figure into a finer one, so a fast balloon cannot hide between samples |
| `failure_stall_seconds` | how long a wedge runs before it is assessed |
| `failure_packages` | `customer-code` and `third-party` guesses into `product` findings — attribution is only as good as this list |

## How to answer with one

Read in this order: `owner` and `blamed_frame` for whose problem it is,
`confidence` for how hard to say it, `capabilities` for whether an absent figure
is unmeasurable or genuinely fine, `fingerprint` for whether it is new, and
`evidence` for the reasoning — which is already written, so quote it rather than
paraphrasing it.

Then answer the question that was actually asked, and lead with the answer —
whether to bump the memory, retry the job, page someone. The incident either
supports what they were about to do or it does not; say which in the first
line, then show the evidence. A caveat that cannot change their decision is
noise however true it is, and an answer they have to scroll through at 2am has
spent the lead time the incident bought them.

Say what you would check next, and separate it from what the run established.
The whole point of these fields is to keep a reader from acting on something
that was never shown.

`.pytest-failures/` on the runner holds whole collections and raw dumps, and
that machine is usually gone by the time anyone reads the alert — which is why
everything above travels in the incident itself.

Recovery callbacks are checkpointed after success under an OS lock. Failed
deliveries retain evidence for retry; deduplicate by run ID and fingerprint
because delivery is at least once. The plugin preserves signal masks and
handlers. Without kernel tracing, a SIGTERM sender remains unknown. A cgroup
OOM counter increase alone does not identify this worker as the victim.
