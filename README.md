# pytest-failure-instrumentation

[![CI](https://github.com/Heknon/pytest-failure-instrumentation/actions/workflows/ci.yml/badge.svg)](https://github.com/Heknon/pytest-failure-instrumentation/actions/workflows/ci.yml)

Most test failures explain themselves. A `pytest_runtest_makereport` gives you
an assertion, a traceback and a node id, and there is nothing left to
investigate.

The failures that happen *outside* the call phase explain nothing. A worker is
killed, a run wedges, workers disagree about which tests exist, pytest raises
inside its own machinery — and what reaches your reporting is a placeholder
string, a line in a log, or nothing at all. This plugin records what those
failures cannot say for themselves, works out whose code is responsible, and
hands you one structured incident per problem.

```
[worker_death] NATIVE_CRASH  severity=critical  owner=product
    blamed on engine.py:6 in native_call
    in flight test_crashes.py::test_crashes  phase=call  started=1 finished=0
    · died while running test_crashes.py::test_crashes (call)
    · exit status -11 - SIGSEGV: segmentation fault in native code (pid 805, via waitid)
    · the worker wrote a stack before dying
    · segmentation fault in native code
```

## The problem

When a pytest-xdist worker dies, this is the whole report:

```
[gw7] node down: Not properly terminated
```

An OOM kill, a segfault in a C extension and a stray `os._exit(1)` are
indistinguishable at that point. Not because nobody bothered to print the
difference — because by the time anything can ask, the difference is gone.
There are three independent reasons, and all three have to be worked around
separately.

**1. The cause never leaves the process.** SIGSEGV and SIGKILL end a process
without running any Python. No `finally`, no `atexit`, no `__del__`, no
`pytest_sessionfinish`. Whatever the worker knew about what it was doing, it
knew only in memory, and that memory is gone. Anything you want to know
afterwards has to have been written down *before* — while the run was healthy
and the cost of writing it lands on every passing test.

**2. The exit status is read and thrown away.** The kernel keeps one number
that separates all these cases, and the parent process is the only thing
allowed to read it. execnet does read it — `Group.terminate` calls
`gw._io.wait()` in `execnet/multi.py` — and discards the return value. Nothing
in xdist asks for it either.

**3. There is no field to put it in.** In `xdist/workermanage.py`,
`process_from_remote` handles the channel closing: it asks execnet for a remote
error, and when there is none — because the remote never got to send anything —
substitutes a literal string:

```python
err = "Not properly terminated"  # lost connection?
```

That string is then passed to `pytest_testnodedown(node, error)` as the error.
The hook is not withholding the cause. By the time it fires, the placeholder is
genuinely all that exists.

### Why you never see a `MemoryError`

The most common way a worker dies is also the one Python is least able to
report. On Linux, `malloc` returning successfully is not a promise that the
memory exists — overcommit hands out address space and resolves it on first
touch. There is no allocation failure for CPython to raise `MemoryError` from.
The process is killed later, from outside, with SIGKILL, which cannot be
caught, blocked or handled.

And the exit status is `-9` for *all* of it: the kernel OOM killer, a cgroup
limit, a cancelled CI job, a `kill -9` from a stray script. There is no
distinct code for "out of memory". The only in-process evidence that separates
them is the cgroup v2 `memory.events` `oom_kill` counter, which is why
`OOM_KILLED` is claimed only when that counter moved during this run, and
`SIGKILLED` — "something killed it, and here is what that could have been" —
when it did not.

### The failures that reach no hook at all

Worker death at least fires a hook. Three others do not.

**A worker that stalls.** `pytest_testnodedown` needs a dead process; a wedged
one is alive. And the controller hears from a worker only when a phase
*completes*, so from outside, a twenty-minute test and a deadlock are the same
event: nothing. The run does not fail — it never ends, and CI kills the job an
hour later with no artifact naming a test.

**Workers that collected different tests.** xdist notices, writes a unified
diff per differing worker into its own log, and aborts. Nothing structured
reaches a hook. With sixty workers and one odd node that is fifty-nine complete
diffs, every one of them naming the majority as the deviation.

**An internal error.** pytest sets `ExitCode.INTERNAL_ERROR`, which is not in
`summary_exit_codes` in `_pytest/terminal.py` — so `pytest_terminal_summary`
never fires for it. Under xdist it is worse: a worker's internal error is
relayed to the controller as a flat string and re-raised there, so the
`INTERNALERROR>` block you read names xdist's frame, not the failure.

## Who this is for

**You ship a library into other people's test suites.** Their run dies and the
bug report names your package. Nothing in the output can confirm or refute it,
and "cannot reproduce" is not an answer anyone accepts. `owner` is the field
that settles it — `product`, `third-party`, `customer-code` or `runtime` — and
it comes from a stack, not from a guess.

**You own CI for a large suite.** Runs fail with no test named. Was that the
OOM killer, or the runner getting reclaimed mid-job? `-9` is identical either
way, and the answer decides whether you buy more memory or file a ticket with
your CI vendor.

**Your suite hangs sometimes.** Nothing fails, the job times out, and there is
no evidence at all because the process that would produce it is the one that is
stuck. `worker_stall` names the test, says whether the thread is blocked or the
whole process is frozen, and prints the stack of the thread actually stuck.

**You run enough workers that they disagree.** A conftest keyed on an
environment variable, a machine-dependent skip, a plugin that collects
conditionally. The run aborts and the reason is buried in N−1 diffs.

**You are collecting failures across machines you do not control.**
`fingerprint` groups recurrences so one defect on twelve workers is one row
with a count, and `capabilities` records what each machine could measure — so a
missing memory figure on a customer's Windows box reads as "unmeasurable here"
rather than "fine".

## A worked example: xdist #1362

A worker dies in the window after it has sent its collection but before
scheduling begins, while a second worker is still collecting. The stale entry
in `registered_collections` is never cleaned up, and the run dies with a
`KeyError` naming an object rather than a problem
([pytest-dev/pytest-xdist#1362](https://github.com/pytest-dev/pytest-xdist/issues/1362)).

This is what the plugin reports for it — two incidents, because two things went
wrong:

```
[worker_death] SIGKILLED  severity=needs-triage  owner=unknown
    no test in flight  started=0 finished=0
    · died before running any test (startup or collection)
    · exit status -9 - SIGKILL: uncatchable kill (OOM killer or external kill) (pid 21780, via waitid)
    · resident memory 31 MB at last checkpoint
    · SIGKILL with no cgroup OOM event: a host-level OOM killer, a container or CI cancellation, or an external kill

[internal_error] INTERNAL_ERROR  severity=high  owner=runtime  run-ending
    blamed on loadscope.py:275 in _assign_work_unit
    KeyError: <WorkerController gw1>
    · raised on the controller itself and captured first-hand
    · raised above informational: a framework-owned defect that ended the run - no test is at fault, so nothing else will surface it
```

`owner=runtime` is the load-bearing part. No test is at fault and no worker is
at fault, so nothing else in the run will ever surface this — which is exactly
why it is the one case where a framework defect is raised *above*
informational.

## Install

```console
pip install pytest-failure-instrumentation
```

It registers itself as a `pytest11` entry point. Implement one hook to receive
what it finds:

```python
# conftest.py, or your own plugin
def pytest_failure_incident(incident):
    database.save(incident.model_dump())
    alerts.send(str(incident))
```

Tell it which packages are yours, so a failing frame in your code can be told
from one in a dependency or in the customer's own tests:

```ini
[pytest]
failure_packages = yourcore, yourcore_ext
failure_product_version = 4.2.0
```

Without the hook it still writes its evidence to `.pytest-failures/`.
Disable it entirely with `-p no:failure_instrumentation`.

## What you get

`incident` is a pydantic model, one class per kind, discriminated on
`incident.kind`. A segfault's resident memory and a run summary's exit code
have nothing to say to each other, so they are not fields of the same object:

| `kind` | Model | Raised on |
|---|---|---|
| `worker_death` | `WorkerDeathIncident` | needs xdist |
| `worker_stall` | `WorkerStallIncident` | needs xdist |
| `collection_mismatch` | `CollectionMismatchIncident` | needs xdist |
| `internal_error` | `InternalErrorIncident` | any run |
| `run_summary` | `RunSummaryIncident` | any run |

The last two are not distributed problems, so the plugin registers whether or
not you run under xdist and a plain `pytest` gets both.

They share `verdict`, `confidence`, `severity`, `owner`, `fingerprint`,
`run_id`, `worker` and `evidence`. `str(incident)` is the alert text — every
block quoted in this README is what it prints. A stored row comes back as the
model it was written from, and the union is a schema you can migrate a table
against:

```python
from pytest_failure_instrumentation.incidents import registry

incident = registry.parse(json.loads(row))   # -> WorkerDeathIncident, ...
registry.json_schema()
```

**`owner`** is the field that settles arguments — `product`, `third-party`,
`customer-code`, `runtime`, or `unknown`. It comes from walking outward past
runtime frames to the first one that belongs to somebody: the deepest frame is
usually `ctypes.string_at`, which tells nobody anything. A stack with no owned
frame at all is not unknown — it is a positive finding that the framework
itself failed.

**`severity`** follows from ownership rather than from how loud the failure
was, so a customer's segfaulting test does not page you. The exception is a
framework defect that ends the run, above.

**`fingerprint`** is stable across runs and excludes worker id, pid and
timings, so one defect on twelve workers is one incident with a count.

**`capabilities`** says what the machine could measure, so an absent figure is
never read as a healthy one.

**`suspect_owner`** is kept apart from `owner` on purpose. When no stack names
anybody, the test that was in flight is a lead worth recording — but a guess
must never sit in the column a reader takes for a finding.

## Verdicts

### A worker died

| Verdict | Told apart by |
|---|---|
| `OOM_KILLED` | `-9` **and** the cgroup OOM counter moved |
| `SIGKILLED` | `-9`, counter flat — host OOM, CI cancellation, external kill |
| `NATIVE_CRASH` | SIGSEGV/SIGABRT/SIGBUS/SIGILL/SIGFPE, or a Windows NTSTATUS |
| `SIGNAL_<n>` | SIGTERM/SIGINT/SIGHUP — a request to stop, not a defect |
| `SELF_EXIT` | any exit code with no signal, `0` included — a worker that left the run was not asked to |
| `PROBABLY_SIGNALLED` | exit code 128–191, a wrapper ate the signal |
| `UNKNOWN` | no status obtainable (remote gateway) |

### A worker stalled

Silence proves nothing on its own: the controller hears from a worker only when
a phase *completes*, so a twenty-minute test and a deadlock look identical from
outside. What separates them is the worker's own heartbeat, which carries CPU
time.

| Verdict | Heartbeat | CPU | Means |
|---|---|---|---|
| `STALLED_BLOCKED` | alive | none | the test thread is waiting on something that is not coming |
| `STALLED_FROZEN` | stopped | — | native code is holding the GIL, or the process is stopped |
| `STALLED_SILENT` | never ran | — | the watchdog is off, so there is no passive evidence either way |
| *(not reported)* | alive | burning | slow, not stuck |

The verdict is reached from beats already on disk. A stack is asked for
*afterwards*, once the decision is made, because asking a wedged process a
question can change its answer — see below.

### Workers collected different tests

| Verdict | Means |
|---|---|
| `COLLECTION_MEMBERSHIP_DIFFERS` | a test exists on one machine and not another |
| `COLLECTION_ORDER_DIFFERS` | same tests, different sequence — fatal too, since xdist addresses tests by position |
| `COLLECTION_PARAMETERS_UNSTABLE` | same tests, different *parameter values* — a parametrize that is not deterministic |

Sixty workers never produce sixty collections. They produce two or three
*variants*, so this reports one row per variant, measured against the largest:

```
[collection_mismatch] COLLECTION_MEMBERSHIP_DIFFERS  severity=needs-triage  owner=unknown  run-ending
    no stack; suspect customer-code (owner of a module the workers disagreed about (test_collect.py))
    2 workers produced 2 different collections
    baseline: 1 worker collected 3 tests, and everything below is measured against that list
    1 worker is missing 1 test, in test_collect.py (gw1)
        - test_collect.py::test_two
    whole collections written to .pytest-failures; the difference above travels in the incident
    · xdist addresses tests by position rather than by id, so any difference between the lists is fatal - a reordering as much as a missing test
    · the initial collections disagreed, so the run was aborted
```

At sixty workers it stays the same shape, because the row count follows the
number of *variants* rather than the number of workers:

```
    58 workers produced 3 different collections
    baseline: 55 workers collected 300 tests, and everything below is measured against that list
    2 workers are missing 1 test, in test_payments.py (gw41, gw58)
        - test_payments.py::test_case_017
    1 worker has 6 extra tests, in test_legacy.py (gw17)
        + test_legacy.py::test_extra_0
        + test_legacy.py::test_extra_1
        + test_legacy.py::test_extra_2
        and 3 more
```

Read it as: **how many distinct opinions existed, which workers held each, and
how the minority differs from the majority.** Magnitude leads each line and
identity follows, samples use diff notation, and a truncated sample always says
how much it withheld — a sample that looks like the whole story is worse than
no sample at all.

**The whole difference travels in the payload**, not just the three ids the
text prints. `missing` and `extra` carry every differing node id, up to 500 per
side, with `missing_count` and `extra_count` as the true totals so you can see
whether that cap was reached. The distinction matters: a *collection* is
unbounded — sixty workers times fifty thousand node ids is hundreds of
megabytes — but a *difference* is almost always one test or one module's worth.
Only the digest is held per worker, and the whole collections are written to
`collection-<digest>.txt` for whoever still has the machine. That file is on a
runner which may be gone by the time anyone reads the alert, which is exactly
why the difference itself does not live there.

An order difference instead reports where the two lists first disagree, which
is the one fact a unified diff of a reordered list destroys.

**A parametrize whose values are drawn at collection time** — `random`, a
timestamp, an unordered set — gives every worker a different id for the same
test, and reported as membership that reads as thousands of tests appearing and
disappearing. It is caught by asking a second question: are these the same
tests once the parameters are stripped from the ids? When they are, the report
names the parametrized tests responsible, drops the per-variant rows — which
would otherwise be one near-identical block per worker — and prints what each
of a few workers actually collected:

```
[collection_mismatch] COLLECTION_PARAMETERS_UNSTABLE  severity=needs-triage  run-ending
    6 workers produced 6 different collections
    the tests are the same on every worker - only the parameter values differ, so these are not tests appearing and disappearing
        test_billing.py::test_invoice
            gw0 collected acct-1791, acct-3471, acct-6305, acct-7468
            gw1 collected acct-2186, acct-2542, acct-6991, acct-9779
            gw2 collected acct-1614, acct-1950, acct-4517, acct-9313
    compare those values: a parametrize evaluated at collection time - a random number, a timestamp, an unordered set, a call to something live - gives every worker a different id for the same test, and xdist requires the ids to match
```

The values are the diagnosis. Naming the test says where to look; three rows
of disjoint account ids say a fetch is running at collection time, and three
rows of floating-point noise say a random number is. Neither is apparent from
one worker's list, which is the only thing xdist ever shows you.

That case is also why full id lists are held for only the first few variants.
"A handful of variants" is the assumption the whole design rests on, and
unstable ids turn it into one variant per worker. Past that limit a variant is
reported as `not compared` rather than diffed against a list nobody kept —
comparing two absent lists reports "the same tests in a different order", which
is a finding invented out of missing data.

A mismatch is run-ending *usually*, not always: xdist aborts when the initial
collections disagree, but silently drops a worker that registers a differing
collection after scheduling has begun. The run then continues one worker short,
and `run_ending` reflects which of the two happened.

## How it knows

**A fixed-size state file.** Which test and phase is open right now is written
to a 256-byte slot with `os.pwrite` — one syscall, no append, no growth, and a
file that is the same size after a million tests as after one. That is what
separates "died in teardown" from "died mid-call": pytest's own `logfinish`
fires only after the whole protocol, so it cannot tell them apart. A node id
too long for the slot has its *tail* trimmed, never the record: a parametrized
id runs past 256 bytes routinely, and truncating the encoded record leaves it
unparseable — which costs the reader the phase and the counters as well, and
reports a worker that died mid-call as one that died before running anything.

**The exit status, taken from the OS.** Where a `Popen` object survives, its
return code. Otherwise `waitid(P_PID, pid, WEXITED | WNOWAIT | WNOHANG)` —
`WNOWAIT` reads the status *without consuming it*, so execnet's own reaping
still works afterwards and nothing is broken by looking. Only a parent may do
this, which is why it happens on the controller, and why a remote gateway
honestly reports `UNKNOWN` rather than guessing. macOS does not expose
`os.waitid` at all and falls back to the `Popen` object, which is why
`capabilities` records the mechanism that answered rather than the one the
platform was assumed to have. On Windows the code is normalised to its
unsigned form first: an NTSTATUS is above 2³¹, so `0xC000013A` arrives signed
or unsigned depending on who answered — and a negative status means "killed by
signal N" to everything downstream.

**faulthandler, pointed at a per-worker file.** pytest's own faulthandler
plugin enables at configure time with `trylast`, aimed at shared stderr where
every worker's output interleaves. This claims the handler back afterwards, in
`pytest_sessionstart`. Its C handler is async-signal-safe and writes *while the
GIL is held* — which is the case that matters, since native code holding the
GIL is exactly what a frozen worker looks like.

**Choosing the right thread out of a dump.** A dump written with
`all_threads=True` holds every thread, and the first one printed in a pytest
worker is this plugin's own heartbeat thread; the second is execnet's receiver.
Reporting the first section would blame the instrumentation for the failure it
came to explain. The section reported is the one the fault or signal reached
(`Current thread`), else the one carrying pytest's runtest protocol, else
anything that is not ours.

**A watchdog the worker arms on itself.** The obvious design has the controller
signal a stalled worker and let faulthandler answer. It has two flaws: Windows
has no `SIGUSR1` and `os.kill` there cannot deliver one, and on POSIX the
signal *perturbs the subject* — PEP 475 makes Python retry on `EINTR`, but a C
extension blocked in a raw syscall need not, so it returns early and the stall
being measured disappears. So each test arms
`faulthandler.dump_traceback_later` instead. It works on every platform,
interrupts no syscall, and still dumps while native code holds the GIL. The
signal path remains as an extra, for asking an already-diagnosed worker for a
fresher stack.

**A heartbeat carrying CPU time.** One line every five seconds per worker,
bounded by wall-clock rather than by how many tests run. `time.process_time()`
in each beat is what turns silence into a verdict: alive with no CPU is
blocked, stopped is frozen, alive and burning is a slow test that must be
reported as nothing at all.

**Evidence written before it is needed.** Every mechanism above puts its output
on disk during the healthy part of the run, because a process that is about to
be killed gets no warning. The controller reads files, never the corpse.

## Cost

A passing test must cost as close to nothing as possible, because that is the
overwhelming majority of what runs.

- Per test: two fixed-size writes to a file that never grows, plus arming a
  watchdog timer (~78 µs). No append log, no `/proc` read, no allocation
  tracking.
- Per 5 seconds, per worker: one heartbeat carrying CPU time and resident
  memory.
- Off by default: `tracemalloc` (needed to attribute an OOM kill to a source
  line) and the live-object census — walking the heap on a worker near its
  ceiling is exactly the instrumentation that makes things worse.
- pydantic is imported on the controller, and only when xdist is active. A
  worker never loads it, so nothing about the per-test path changed when the
  payload became typed.
- Nothing in the reporting path may raise. A failure while gathering an
  incident degrades it to what survived, because an exception in a reporting
  hook becomes an `INTERNALERROR` that ends the customer's run.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `failure_packages` | — | Your top-level packages, for attribution |
| `failure_directory` | `.pytest-failures` | Where evidence is written |
| `failure_watchdog` | `true` | Memory and liveness sampling |
| `failure_heartbeat_interval` | `5.0` | Seconds between liveness beats |
| `failure_tracemalloc_depth` | `0` | 1 names the allocating line for OOM attribution |
| `failure_object_census` | `false` | Count live objects at a high-water mark |
| `failure_high_water_mb` | auto | Memory mark for a snapshot; defaults to a share of the discovered limit |
| `failure_memory_limit_mb` | `0` | Soft cap (POSIX) turning an OOM kill into a `MemoryError` |
| `failure_slow_test_seconds` | `120` | A test outliving this dumps its own stack |
| `failure_stall_seconds` | `300` | Silence before a stall is assessed |
| `failure_stack_probe` | `true` | Ask a diagnosed stalled worker for a fresh stack (POSIX) |

`failure_memory_limit_mb` is worth a note: an `RLIMIT_AS` cap makes the
allocation fail *inside* the process, so you get a `MemoryError` with a
traceback and a node id instead of an uncatchable kill with neither. It costs
you a hard ceiling per worker, which is why it is opt-in.

## Platform coverage

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| Test in flight, phase, exit status | yes | yes | yes |
| Crash stack | yes | yes | yes |
| Stack from a *slow or hung* test | yes | yes | yes |
| Current memory | procfs | psutil, else peak only | psapi |
| Container limit, OOM counter | yes | n/a | n/a — no OOM killer |
| On-demand stack from a stalled worker | yes | yes | no |

Two Windows differences are worth knowing about, because they change what you
will see rather than how it is reported.

ctypes wraps every foreign function call in structured exception handling, so
an access violation raised *through ctypes* comes back as an `OSError` and the
worker survives it. A fault inside a real C extension still ends the process —
but the reproduction that segfaults a worker on Linux may simply fail a test on
Windows.

And a Windows process that dies from a fault reports an NTSTATUS as its exit
code rather than a signal, while `abort()` reports plain `3` — the same code a
deliberate `os._exit(3)` gives. What separates a crash from a clean exit there
is whether a dump was written, not the exit status, which is why the crash
stack is evidence in its own right rather than a decoration on the verdict.

`psutil` is never required, only ever an upgrade: `pip install
pytest-failure-instrumentation[psutil]`.

## Tests

```console
pip install -e ".[test]"
pytest
```

The integration tests run a real pytest in a subprocess through `pytester`,
crash or wedge a worker for real, and read back what the plugin raised — so
they exercise the mechanism rather than a mock of it. Every one of them also
round-trips its incidents through `registry.parse` and asserts `model_dump()`
equals the stored row, which makes the payload contract a property of every
scenario rather than a test of its own.

CI runs the suite on Linux, macOS and Windows across Python 3.9–3.13 — every
platform path in the table above is executed on the platform it was written
for. The
probes are platform code — procfs, psapi, `waitid`, `GetExitCodeProcess`,
cgroup counters — and none of the Windows or macOS paths can be exercised on a
Linux runner, which is the whole reason the matrix exists. Two axes matter as
much as the operating system, so each gets its own job:

- **without `psutil`**, which is what most people actually have. Every probe
  has to degrade to a declared "unavailable" rather than to a wrong number.
- **without `pytest-xdist`**, where `pytest_testnodedown` has no hookspec at
  all and an unspecced hookimpl is a registration error — the failure mode that
  once made a plain `pytest` run report nothing.
- **against the declared minimums**, `pytest==7.0.1` on Python 3.9. Every other
  job installs whatever is newest, so a hook signature or an ini type that
  arrived later would pass all of them and fail on a user's pinned pytest.

## Status

All five kinds and every verdict in the tables above are covered, on all three
platforms.

Most are produced for real: a worker is crashed, killed, signalled, wedged or
made to disagree about its collection, and the incident is read back from the
hook. Two cannot be, by anyone: `OOM_KILLED` needs a kernel that has just
killed something, and `UNKNOWN` needs a remote gateway with no local process to
query. Those branches are exercised against a constructed incident instead — as
are the Windows NTSTATUS decodes, which additionally run against a process that
really exits with one.

The opt-in paths are covered too: the memory ceiling turning an uncatchable
kill into a `MemoryError` that names the test, and the high-water snapshot
naming the line holding the memory.

The probes are also called directly, because in normal use they shadow each
other — psutil answers before psapi, and execnet's `Popen` before `waitid` — so
the fallbacks a customer's machine actually runs were never being executed.
That includes the claim `WNOWAIT` rests on: the status is read, and the process
is still reapable afterwards with the same answer.

The first cross-platform run paid for itself twice. It found that a Windows
`\Lib\` in `sysconfig` and a `\lib\` in a traceback made every stdlib frame
look like nobody's code, so a blocked test was blamed on `threading.py` and
then on the customer who called it — a runtime frame reported as customer code,
which is the one direction this must never fail in. Only the 3.9 cell caught
it. And it found that ctypes cannot raise an uncaught fault on Windows at all,
which is a fact about what users will see rather than about the plugin.
