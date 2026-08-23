---
name: reading-failure-incidents
description: Read and triage an incident raised by pytest-failure-instrumentation - the enriched alerts for pytest failures that happen outside the call phase (worker death, worker stall, collection mismatch, internal error, run summary). Use when an alert block starting with [worker_death], [worker_stall], [collection_mismatch], [internal_error] or [run_summary] appears in CI output or a bug report, when a stored incident payload or pytest_failure_incident hook argument needs interpreting, or when asked what a verdict, owner, severity, confidence or fingerprint field means. Not for ordinary assertion failures, which explain themselves.
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

```
[worker_death] NATIVE_CRASH  severity=critical  owner=product
    blamed on engine.py:6 in native_call
    in flight test_crashes.py::test_crashes  phase=call  started=1 finished=0
    · died while running test_crashes.py::test_crashes (call)
    · exit status -11 - SIGSEGV: segmentation fault in native code (pid 805, via waitid)
    · the worker wrote a stack before dying
```

| Line | Is |
|---|---|
| `[kind] VERDICT severity= owner=` | the headline; `run-ending` is appended when the session died with it, and `owner=` is omitted on a `run_summary`, where nothing failed |
| `blamed on file:line in func` | `blamed_frame` — the first frame on the stack owned by somebody |
| *or* `no stack; suspect X (basis)` | `suspect_owner` — a lead, not a finding |
| unprefixed lines | the kind's own facts |
| `· …` lines | `evidence` — what the verdict was reached from |

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
| `exitstatus` on a `run_summary` | the run's outcome | sometimes reported before pytest applies `INTERNAL_ERROR`; `run_ending_incidents` is the one to trust when they disagree |

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
`customer-code`/`runtime`→informational, `unknown`→needs-triage. Two overrides:
a `run_summary`, and a `SIGNAL_*` identified with high confidence, are
informational; a framework defect that ended the run is raised to high, because
no test is at fault and nothing else will ever surface it. `needs-triage` means
"somebody has to look", not "this is bad".

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
| `OOM_KILLED` | `-9` **and** the cgroup OOM counter moved during this run | memory: the workload, the limit, or worker count |
| `SIGKILLED` | `-9`, counter flat | something outside the container: host-level OOM, CI/container cancellation, runner preemption, an external kill |
| `NATIVE_CRASH` | fatal signal, or a Windows NTSTATUS | the blamed frame — a C extension or a ctypes call |
| `SIGNAL_<n>` | SIGTERM/SIGINT/SIGHUP | nothing, unless the run was not meant to be stopped |
| `SELF_EXIT` | clean code, no signal | `sys.exit()`, `os._exit()`, or a plugin aborting |
| `PROBABLY_SIGNALLED` | exit code 128–191 | a wrapper that ate the signal; the true one did not survive |
| `UNKNOWN` | no status obtainable (remote gateway) | nothing — do not guess one |

`-9` alone never licenses "we ran out of memory"; only the cgroup counter does,
and only when `capabilities.cgroup_oom_counter` says it was readable. That
distinction is the reason both verdicts exist.

Two absences carry information here. No `of a N MB cgroup limit` clause means
no container limit was discovered, so raising one may change nothing. And no
`system had N MB free` line means no high-water snapshot ever fired — the
worker never came near a ceiling — which is evidence against memory in its own
right.

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

Two things follow from `run_ending` being true here. The run has no path to
completion, because xdist waits for work it handed out and never gets back — so
a job that hangs to its timeout is the symptom and the stall is the cause. And
the incident was raised at the threshold, often an hour before that timeout, so
the useful advice is usually to act on the incident rather than wait.

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

## The decision each kind forces

Every one of these reaches a person who has to do something before morning.
The incident usually settles it, and saying so is most of the value.

| Kind | Already true when you read it | What it argues for |
|---|---|---|
| `worker_death` | xdist started a replacement; the run went on | fix or route by `owner`; a retry is reasonable only when the cause was external (`SIGNAL_*`, a cancellation) |
| `worker_stall` | the run has no path to completion and is burning runner time until a timeout | kill the job now rather than waiting it out. A `STALLED_BLOCKED` deadlock is deterministic, so a retry hangs the same way — the incident is worth more than the rerun, and it already holds the live stack you cannot get afterwards |
| `collection_mismatch` | the run aborted, or quietly lost a worker | retrying repeats it — an unstable parametrize re-rolls its values, an environment-dependent collection diverges the same way again. Nothing improves until collection is deterministic |
| `internal_error` | the session is over | nobody's test is at fault, so no test-level triage will surface it — it needs the framework or plugin owner |
| `run_summary` | the run ended cleanly | nothing, except as the control: its absence next to another incident means the controller died too |

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
