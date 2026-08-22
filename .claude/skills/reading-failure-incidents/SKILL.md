---
name: reading-failure-incidents
description: Read and triage an incident raised by pytest-failure-instrumentation - the enriched alerts for pytest failures that happen outside the call phase (worker death, worker stall, collection mismatch, internal error, run summary). Use when an alert block starting with [worker_death], [worker_stall], [collection_mismatch], [internal_error] or [run_summary] appears in CI output or a bug report, when a stored incident payload or pytest_failure_incident hook argument needs interpreting, or when asked what a verdict, owner, severity, confidence or fingerprint field means. Not for ordinary assertion failures, which explain themselves.
---

# Reading a failure incident

An incident is one structured record per problem, raised through the
`pytest_failure_incident` hook. It has two forms and they carry the same facts:

- **The alert text** — `str(incident)`. What lands in Slack, a log or a bug
  report. Indented block, headline first.
- **The payload** — a pydantic model, one class per `kind`. What a database
  stores. Parse a stored row back with
  `pytest_failure_instrumentation.incidents.registry.parse(row)`;
  `registry.json_schema()` is the contract for the whole union.

Everything the alert prints is in the payload. The reverse is not true — the
text is trimmed for a reader, so **for anything quantitative (full id lists,
memory figures, counts), read the payload, not the text.**

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
| *or* `no stack; suspect X (basis)` | `suspect_owner` — **a lead, never a finding** |
| unprefixed lines | `details()`, the kind's own facts |
| `· …` lines | `evidence` — what the verdict was reached from |

## The shared fields

**`owner`** — `product`, `third-party`, `customer-code`, `runtime`, `unknown`.
Whose code is on the stack, found by walking outward past runtime frames to the
first frame belonging to someone. `runtime` is a positive finding — no test code
anywhere on the stack, so the framework itself is what failed — not a missing
answer. Only `unknown` means nothing was determined.

**`severity`** — follows from `owner`, not from how violent the failure was:
`product`→critical, `third-party`→high, `customer-code`/`runtime`→informational,
`unknown`→needs-triage. So a customer's segfaulting test is informational by
design. Two overrides: a `run_summary`, and a `SIGNAL_*` identified with high
confidence, are informational; a framework defect that ended the run is raised
to high, because no test is at fault and nothing else will ever surface it.

**`confidence`** — `high`, `medium`, `low`, about the *verdict*. Never restate a
low-confidence verdict as fact.

**`fingerprint`** — stable across runs, excludes worker id, pid, timings and
memory. One defect on twelve workers is one incident with a count. Use it to
group recurrences; never as an identifier of a single occurrence.

**`suspect_owner` / `suspect_basis`** — set only when no stack named anybody.
Report it as a lead ("the test in flight was X"), never in the sentence where a
reader expects `owner`.

**`capabilities`** — what the machine could measure. Before concluding anything
from an absent figure, check here: a missing memory number on Windows means
unmeasurable, not healthy. Same for `stack_probed=False` on a stall.

**`INSTRUMENTATION_FAILED`** as a verdict means gathering the incident raised.
The underlying failure was real; only the detail is missing.

## Per kind

### `worker_death` — the process ended when it should not have

xdist's own report is `node down: Not properly terminated`. The verdict is what
replaces it:

| Verdict | Means | Act on |
|---|---|---|
| `OOM_KILLED` | `-9` **and** the cgroup OOM counter moved during this run | memory: the workload, the limit, or worker count |
| `SIGKILLED` | `-9`, counter flat | host OOM, container/CI cancellation or an external kill — the difference is not in the process |
| `NATIVE_CRASH` | fatal signal or a Windows NTSTATUS | the blamed frame; a C extension or ctypes call |
| `SIGNAL_<n>` | SIGTERM/SIGINT/SIGHUP | nothing, unless the run was not meant to be stopped |
| `SELF_EXIT` | clean code, no signal | something called `sys.exit()`/`os._exit()`, or a plugin aborted |
| `PROBABLY_SIGNALLED` | exit code 128–191 | a wrapper ate the signal; the true one did not survive |
| `UNKNOWN` | no status obtainable (remote gateway) | do not guess one |

`-9` alone never licenses "out of memory" — only `cgroup_oom_kills_since_start`
does. Read `test_in_flight` with `phase` (`setup`/`call`/`teardown`); no test in
flight with `tests_started=0` means it died during startup or collection.

### `worker_stall` — alive, but stopped reporting

Silence proves nothing on its own; the controller hears from a worker only when
a phase completes. The heartbeat's CPU time is what separates the cases.

| Verdict | Means |
|---|---|
| `STALLED_BLOCKED` | heartbeat alive, no CPU — waiting on something that is not coming |
| `STALLED_FROZEN` | heartbeat stopped — native code holding the GIL, or the process stopped |
| `STALLED_SILENT` | no heartbeat ever ran (watchdog off) — no passive evidence either way, `confidence=low` |

A merely slow test (alive, burning CPU) is never reported. `stack` is asked for
*after* the verdict and may be absent: `stack_probed=False` means the platform
was not asked (Windows, or probing disabled), which is a fact about the machine.

### `collection_mismatch` — workers disagree about which tests exist

Read as: **how many distinct opinions existed, who held each, and how the
minority differs from the majority.** Rows follow `variant_count`, not worker
count. `role="baseline"` is the largest group; everything else is measured
against it.

| Verdict | Means |
|---|---|
| `COLLECTION_MEMBERSHIP_DIFFERS` | a test exists on one machine and not another |
| `COLLECTION_ORDER_DIFFERS` | same tests, different sequence — fatal too, xdist addresses tests by position |
| `COLLECTION_PARAMETERS_UNSTABLE` | same tests, different parameter values — a parametrize evaluated at collection time (`random`, a timestamp, an unordered set, a live call) |

Three traps:

- The text prints a few ids; `missing`/`extra` carry up to 500 per side, and
  `missing_count`/`extra_count` are the **true** totals. Compare them before
  saying how big the difference is.
- A variant with `compared=False` or `kind="uncompared"` was never diffed. Do
  not describe it as agreeing or as reordered — nothing was compared. Full id
  lists are held for the first five variants only, which is what runs out.
- For `COLLECTION_PARAMETERS_UNSTABLE` the diagnosis is in
  `parameter_samples`: comparing what each worker collected for the same test
  says *which* nondeterminism it is. Disjoint ids mean a live fetch; float noise
  means a random number.

`run_ending` is not constant here — xdist aborts when the *initial* collections
disagree, and silently drops a late replacement worker instead. The field says
which happened.

### `internal_error` — pytest raised inside its own machinery

Always run-ending, and pytest fires no terminal summary for it, so nothing else
reports it. Check **`first_hand`**: `False` means this is xdist's re-raise of a
worker's error, so the traceback names xdist's frame rather than the failure and
worker attribution is unreliable. `exception` is the real
`SomeError: message` line; `detail` is the traceback, tail-truncated.
`owner=runtime` plus run-ending is the case severity raises to high.

### `run_summary` — one per run, whose *absence* is the finding

`verdict=RUN_FINISHED`, always informational, emitted for single-process runs
too. It says the reporting process reached the end — **so a run with no summary
is a run whose controller died**, which nothing inside that process could tell
you. `incidents` maps fingerprint → count for the run; `raised` and
`duplicates_suppressed` are totals. If `exitstatus` is 0 while
`run_ending_incidents` is non-zero, trust the latter: pytest sometimes reports
the status before applying `INTERNAL_ERROR`.

## Triage checklist

1. `owner` and `blamed_frame` — whose problem is it? Do not upgrade
   `suspect_owner` into an answer.
2. `confidence` — hedge a `low` or `medium` verdict in the words you use.
3. `capabilities` — is an absent figure unmeasurable, or genuinely fine?
4. `fingerprint` — has this been seen before? One row, one count.
5. `evidence` — quote it. It is the reasoning, already written.
6. Anything quantitative — take it from the payload, not the alert text.

`.pytest-failures/` on the runner holds whole collections and raw dumps. That
machine is usually gone by the time the alert is read, which is why everything
above travels in the incident itself.
