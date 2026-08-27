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

## Installing it from your own framework

If you ship a test framework rather than consume one, ini is the wrong place
for these settings. Your packages, your artifact directory and your build id
are things your framework already knows, and an ini block every consuming team
has to copy — and keep in step with you — is a migration that never finishes.

Call `install` from your plugin instead:

```python
# yourframework/pytest_plugin.py
from pytest_failure_instrumentation import install

def pytest_configure(config):
    install(config,
            packages=deployment.owned_packages,
            directory=deployment.artifact_dir,
            product_version=deployment.version,
            run_id=ci.build_id)
```

Keyword arguments layer on top of whatever ini said, so a team that has set
`failure_stall_seconds` keeps it. Passing a whole `Settings` instead replaces
ini entirely, for when your framework owns the configuration outright:

```python
from pytest_failure_instrumentation import Settings, install

install(config, Settings(packages=("yourcore",), stall_seconds=600))
```

`Settings` has a default for every field, coerces a list of packages and a
string path to what it needs, and enforces its own invariants — so a
hand-built one cannot skip a floor that a resolved one obeys.

Three things this handles for you:

**Load order.** A conftest's `pytest_configure` runs before this plugin's, and
a plugin loaded as an entry point runs *after* it. Registration here is
`trylast` and only claims what nobody has installed, so your settings win
either way.

**Workers.** A worker is a separate process. Settings you computed in Python do
not exist there and your framework's code may not even be loaded — so whatever
is in force is pushed down through `workerinput`, and a worker prefers it over
anything it could read itself. `run_id` travels with it, which is what lets a
build id group a whole run's incidents.

**Turning auto-registration off.** `-p no:failure_instrumentation` skips the
entry point entirely; `install` puts back the hookspecs so
`pytest_failure_incident`, `pytest_failure_worker_sample` and
`pytest_failure_server_ready` all still reach their implementers. Note that it also skips `pytest_addoption`, so
`failure_*` ini keys become unknown config options — which is the point if your
framework owns the settings, and a reason to leave the entry point enabled and
just call `install` if it does not.

`install` is idempotent and returns the settings in force. A second call keeps
the first one's and warns rather than silently losing to it;
`installed_settings(config)` reads them back. Like everything else here it
warns instead of raising — the one exception is a misspelled setting name,
which is your bug and is worth an error rather than a run that quietly
attributes nothing.

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

**The stack** is in the payload but out of `str(incident)`, because forty
frames turn a readable incident into a wall and whether they belong in an alert
is your call. `incident.raw_stack()` returns them as lines whatever the kind is;
`top_frame` and `blamed_frame` are the two already parsed, each with `file`,
`line`, `function`, `module` and `owner`.

What you get is the deepest frames of *one* thread from the most recent dump —
the other threads in a worker are this plugin's own heartbeat and execnet's
receiver, and reporting those blames the instrumentation. It is capped (40
frames for a death, 14 for a stall, 4000 characters for an internal error) and
a cut stack ends with `... and N more frames` rather than pretending to be
whole. The complete dump stays in `<worker>.crash` on the runner.

```python
def pytest_failure_incident(incident):
    body = str(incident)
    frames = incident.raw_stack()
    if frames:
        body += "\n\n" + "\n".join(frames)
    alerts.send(body)
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

### Handing one to an agent

An incident is written to be read without context, which is most of what an LLM
triaging a CI failure lacks. `.claude/skills/reading-failure-incidents/SKILL.md`
is that context in one file: the anatomy of the alert text, what each shared
field licenses a reader to conclude, the verdicts per kind, and the handful of
places where the text is a summary and the payload is the number — so an agent
reports what the incident found rather than what a `-9` looks like.

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
to a fixed-size slot with `os.pwrite` — one syscall, no append, no growth, and a
file that is the same size after a million tests as after one. That is what
separates "died in teardown" from "died mid-call": pytest's own `logfinish`
fires only after the whole protocol, so it cannot tell them apart. The slot is
5 KiB, holding a node id of around 4950 characters whole — past any real one by
an order of magnitude, since a path, a class, a test name and a couple of
content hashes together use a twentieth of it. The size is close to free: one
write of one buffer costs the same syscall from 256 bytes to 8 KiB, and 5 KiB
per worker is 320 KiB across a 64-way run. An id longer than that gives up its
*middle*, never the record: truncating the encoded record leaves it
unparseable, which costs the reader
the phase and the counters as well and reports a worker that died mid-call as
one that died before running anything. The middle goes rather than the tail
because both ends carry something the other does not — the head is the module
attribution reads, and the tail is where a parametrize puts the value saying
which case this was.

The slot carries *two* node ids, and the difference is why it is read at all.
`test_in_flight` is the test currently running and is cleared when teardown
returns; `last_test` is the most recent test whether or not it finished. One
field cannot be both, and being both is how a worker that died in the gap
between two tests came to be reported as having died *in* the one that had
already passed — attributed to whoever owns it, with an owner and a severity on
it. Where nothing is in flight the incident says so, and the lead it offers
names itself as the last test rather than the running one.

It carries the run id too. The evidence directory outlives a run and clearing
it is best-effort — on Windows a file another process still has open cannot be
unlinked at all — so a record stamped with a different run is refused rather
than read as this one's. That check is load-bearing beyond the report: the pid
in a stale record is a pid this run's stack probe would otherwise signal.

**The exit status, taken from the OS.** Where a `Popen` object survives, its
return code. Otherwise `waitid(P_PID, pid, WEXITED | WNOWAIT | WNOHANG)` —
`WNOWAIT` reads the status *without consuming it*, so execnet's own reaping
still works afterwards and nothing is broken by looking. Only a parent may do
this, which is why it happens on the controller, and why a remote gateway
honestly reports `UNKNOWN` rather than guessing. macOS does not expose
`os.waitid` at all and falls back to the `Popen` object. `capabilities.exit_status`
records which mechanism this machine *has*; `exit_status_source` on the incident
records which one actually answered — so a figure is never read as having come
from a mechanism that was never used. On Windows the code is normalised to its
unsigned form first: an NTSTATUS is above 2³¹, so `0xC000013A` arrives signed
or unsigned depending on who answered — and a negative status means "killed by
signal N" to everything downstream.

**faulthandler, pointed at a per-worker file.** pytest's own faulthandler
plugin enables at configure time with `trylast`, aimed at shared stderr where
every worker's output interleaves. This claims the handler back afterwards, in
`pytest_sessionstart`. Its C handler is async-signal-safe and writes *while the
GIL is held* — which is the case that matters, since native code holding the
GIL is exactly what a frozen worker looks like.

**Separate dump files.** A fatal dump goes to `<worker>.crash`; the slow-test
watchdog's goes to `<worker>.slow`, and the frozen-interpreter fallback's to
`<worker>.frozen`. They are the same shape and only
the banner separates them — `Fatal Python error` against `Timeout (…)` — and a
watchdog dump is written by tests that go on to *pass*. Sharing one file made
"a stack exists" ambiguous, and on the Windows path where the dump is the only
thing distinguishing `abort()` from `os._exit(3)`, ambiguous means a slow test
that passed can be reported as the crash that killed the worker, blamed on
whatever that stack happened to be doing.

**Choosing the right dump, and the right thread inside it.** Two things have to
be picked here, and getting either wrong blames code that was not running.

A file holds as many dumps as were written to it, and the crash file
accumulates: an on-demand stack taken while a worker was merely stalled
precedes the fatal dump that ends it. The dump that describes the present is
the *last* one. The watchdog's file holds only ever one, because each dump is
written beside it and renamed into place — a reader that caught it mid-write
would get the threads faulthandler had reached and not the one running the
test.

Within a dump, `all_threads=True` means every thread is present, and the first
printed in a pytest worker is this plugin's own heartbeat thread; the second is
execnet's receiver. Reporting the first section would blame the instrumentation
for the failure it came to explain. The section reported is the one the fault
or signal reached (`Current thread`), else the one carrying pytest's runtest
protocol, else anything that is not ours. `Current thread` is skipped when it
is *ours*: the watchdog's dump is taken by the heartbeat thread, so
faulthandler labels that one current, and believing the label reported the
heartbeat as the frozen test.

**Saying how old a stack is.** A stack is evidence about a moment, and the
frames look the same whether they were taken just now or left behind four
minutes ago. A stall that could be probed reports a current stack; one that
falls back to the watchdog's file says `stack written 47s ago by the slow-test
watchdog, not taken just now`, and carries `stack_age_seconds`. A death reports
`crash_stack_age_seconds` alongside, so a dump that predates the death reads as
the context it is rather than as the crash.

**A watchdog on a cadence, written by the heartbeat thread.** A test still
running after `failure_slow_test_seconds` has its stack written for it, and
keeps having it rewritten every interval, so whatever is on disk is at most one
interval old. That bound is the point: on Windows nothing can ask a live
process for a stack, so this is the only one a stalled worker will ever have.
The clock starts at *setup* and stops at the end of *teardown*, so a fixture
blocking on a container and a finalizer blocking on a connection are covered as
well as the test body — those are the commonest real hangs there are, and the
state slot has always told them apart. Once for the whole test rather than per
phase, or a test that spent most of the interval in setup and the rest in the
call would never reach it. The default is 20 seconds, and the file is dropped
when the test ends, so only the running test's stack is ever on disk (about
5 KB) and a healthy suite leaves nothing.

The obvious design instead has the controller signal a stalled worker and let
faulthandler answer. It has two flaws: Windows has no `SIGUSR1` and `os.kill`
there cannot deliver one, and on POSIX the signal *perturbs the subject* —
PEP 475 makes Python retry on `EINTR`, but a C extension blocked in a raw
syscall need not, so it returns early and the stall being measured disappears.
The signal path remains as an extra, for asking an already-diagnosed worker for
a fresher stack.

The next design, and what this was until it was measured, is
`faulthandler.dump_traceback_later(repeat=True)`. It needs no signal and works
on Windows, and it dumps even while native code holds the GIL — but it dumps
from a C thread that does *not* hold the GIL, walking every other thread's
frames while those threads push and pop them. A dump landing while the
interpreter is executing rather than blocked reads a frame being torn down, and
the worker segfaults. Over a suite whose tests were four times the cadence
long, that killed the worker in 10 runs out of 10, against 0 with the repeat
turned off; it left the dump ending mid-frame with a nonsense line number, and
the crash file empty because the fault was inside the dumper. Instrumentation
that crashes what it is watching is worse than no instrumentation, so the
cadence is driven from the heartbeat thread instead, in Python, holding the
GIL — nothing else can be mutating what is being walked.

**A fallback for the one stack a Python thread cannot take.** When native code
holds the GIL, no Python thread runs and the watchdog above writes nothing.
That is the case the C timer exists for, and also the case that makes it
dangerous — so it is armed such that it can only fire when it is safe. Every
heartbeat pushes its deadline out by three intervals, so while Python runs at
all the deadline is always in the future and the timer never fires; when three
beats in a row are missed it fires once, and by then nothing is executing for
it to trip over. Missing beats for that long has one realistic cause: a thread
holding the GIL and running C. A main thread running Python releases the GIL
every few milliseconds, and a machine loaded badly enough to starve a daemon
thread for three intervals would starve the timer's own thread with it. The
dump goes to `<worker>.frozen`, because it means something the watchdog's does
not — not "this test is slow" but "this process stopped responding" — and the
incident says which file its stack came out of in `stack_source`.

**It stands down where pytest is using that timer.** There is exactly one
`faulthandler.dump_traceback_later` timer per process, and arming it cancels
whatever was armed before. pytest's own faulthandler plugin arms it at the
start of every test when `faulthandler_timeout` is set, and this fallback
re-arms every second — so the fallback always won, and a configured timeout
silently never fired, `faulthandler_exit_on_timeout` and all. Where that ini is
set the fallback is not armed at all, and the worker records
`frozen_fallback_stood_down` in its event log saying why. It costs a stalled
worker its frozen-fallback stack, which is a worse report; the alternative is a
run that hangs past a timeout somebody configured, which is a worse run.

**Nothing is signalled that cannot be confirmed.** `SIGUSR1`'s default
disposition is to *terminate*, and the pid the on-demand probe would signal was
read back out of a file the worker wrote. A worker that has since exited leaves
its number to be handed on by the kernel, so signalling it does not produce a
bad report — it kills an unrelated process. The pid is signalled only once
something says it is still ours: the controller can see that process running
under this worker's gateway, or the machine can be asked and answers that it is
a child of this one. A machine that cannot be asked at all is not taken as a
yes, and the incident says which of those it was instead of showing a stack it
never had.

**A heartbeat carrying CPU time.** One line every five seconds per worker,
bounded by wall-clock rather than by how many tests run. `time.process_time()`
in each beat is what turns silence into a verdict: alive with no CPU is
blocked, stopped is frozen, alive and burning is a slow test that must be
reported as nothing at all.

**Evidence written before it is needed.** Every mechanism above puts its output
on disk during the healthy part of the run, because a process that is about to
be killed gets no warning. The controller reads files, never the corpse.

## Live stacks over HTTP

Everything above is for reading afterwards. This is the other direction: a UI
watching a run, asking what a test is doing *while it is still doing it*.

```console
$ curl -H "Authorization: Bearer $TOKEN" localhost:8080/stack?pid=48213
{"pid": 48213, "source": "py-spy", "captured_at": 1756142887.31,
 "threads": [{"thread_id": 8632442880, "thread_name": "MainThread",
              "owns_gil": true, "active": true,
              "frames": [{"function": "_wait_for_lease", "file": "/app/pool.py", "line": 91},
                         {"function": "checkout", "file": "/app/pool.py", "line": 44},
                         {"function": "test_concurrent_writes", "file": "/tests/test_pool.py", "line": 210}]}]}
```

Off by default — a plugin installed for crash reporting should not start
opening listening sockets on everybody who upgrades it:

```ini
[pytest]
failure_stack_server = true
```

```console
$ pytest --callstack-port 8080          # also switches it on
$ pytest --callstack-host 0.0.0.0       # so does this
```

### Two modes, and the port number picks between them

**Drawn** — the default, when no port is named. The session binds whatever the
kernel hands it and writes the address into the evidence directory. Nothing is
shared, so nothing is contended and nothing can be lost to another session.

**Named** — `--callstack-port 8080`, or the ini equivalent. The session claims
that exact port and shares it with every other session on the machine, since a
fixed port cannot be bound twice. First to start serves; the rest wait, and take
over within five seconds of the holder exiting.

Name a port when something outside has to be told the address once and for all —
a firewall rule, a UI with it compiled in, a published container port. Otherwise
let one be drawn: a UI that can read the evidence directory needs no agreement
about numbers, and it has to read that directory anyway to know which pid is
running which test.

### What is running where

`GET /workers` answers the whole question in one request, assembled from files
the run was writing anyway — no ptrace, no per-test cost, nothing written:

```json
{"served_by": {"service": "…", "pid": 17155}, "observed_at": 1787688175.421,
 "runs": [{"session": "run-19d52c2ff8e2", "run_id": "757f3cc51790…",
           "controller": {"pid": 17155, "alive": true},
   "workers": [
     {"worker": "gw0", "pid": 21615, "nodeid": "test_slow.py::test_alpha", "phase": "call",
      "status": "blocked", "why": "heartbeat 0.5s old but no CPU progress: the test thread is waiting on something",
      "process_exists": true, "heartbeat_age_s": 0.5, "cpu_rate": 0.001, "rss_mb": 32},
     {"worker": "gw1", "pid": 21618, "nodeid": "test_slow.py::test_beta", "phase": "call",
      "status": "gone", "why": "process 21618 no longer exists; last seen in call of test_slow.py::test_beta",
      "process_exists": false},
     {"worker": "gw2", "pid": 21621, "nodeid": "test_slow.py::test_gamma", "phase": "call",
      "status": "working", "why": "heartbeat 0.3s old, burning 1.00 cores", "cpu_rate": 1.0}]}]}
```

`?worker=` narrows it to particular workers, which on a sixty-four-way run is
the difference between reading one state file and reading all of them. Both
spellings and both shapes work, and they mix:

```console
$ curl 'localhost:8080/workers?worker=gw1'
$ curl 'localhost:8080/workers?worker=gw0,gw3'
$ curl 'localhost:8080/workers?worker=gw0&worker=gw2'
```

Runs left with no matching worker drop out, and names that matched nothing
anywhere come back under `filter.unmatched` — otherwise a caller cannot tell
"not running" from "misspelt". An empty `?worker=` is treated as no filter,
because that is what a UI sends when its filter box is empty. The names are
compared against a directory listing and never joined onto one, so a value that
looks like a path is just a name that matches nothing.

The status vocabulary is [`analysis/stall.py`](#how-it-knows)'s truth table, as
a live status rather than a post-hoc verdict:

| status | heartbeat | CPU | process |
|---|---|---|---|
| `working` | fresh | above 0.05 cores | exists |
| `blocked` | fresh | below that | exists |
| `frozen` | stale | — | exists |
| `gone` | — | — | absent |
| `unmeasured` | never any | — | — |

Three files answer three different questions, and keeping them apart is what
makes this cheap and correct. `.state` says *what* a worker is doing and is
written before each phase runs, so it is ahead of anything the controller
knows — but a twenty-minute test writes nothing for twenty minutes, so a stale
record says nothing about liveness. `.events` carries a heartbeat every few
seconds whatever the test is doing, and the CPU time on each beat is the only
thing separating a worker that is *working* from one that is *stuck*. The pid
answers the narrowest question of the three, about a number that can be reused.

Two details that are easy to get wrong and are handled here:

- **A killed worker is a zombie until its parent reaps it**, and the kernel
  accepts signals for it the whole time — so a `kill(pid, 0)` check reports a
  worker killed a moment ago as alive, which is the opposite of what a crash
  view is for. Linux answers from procfs, which is cheaper than building a
  psutil object per worker per request; everywhere else psutil answers.
- **Liveness is a different mechanism per platform.** Signal 0 is a POSIX
  question; on Windows it is not a question at all (see below), so the platform
  picks the mechanism before anything else happens.
- **`cpu_rate: null` is not zero.** "It burned nothing" and "we could not
  measure" are different findings, and a worker at full tilt whose beats
  collide produces the second.

Nothing here signals a worker or asks it anything: every verdict comes from
beats already on disk, because asking a wedged process a question can dissolve
the stall you were measuring.

`nodeid` and `phase` are `null` between tests, and a very long `nodeid` is
trimmed from both ends with `nodeid_elided: true` saying so.

### When there is no live view

Switching the server on and getting no server raises a
`stack_server_unavailable` incident through the same hook as everything else.
Without it the run continues perfectly well and your UI shows nothing forever
with no error anywhere — because from the outside "no server" and "no tests
running" look identical, which is the exact misreading this package exists to
prevent:

```
[stack_server_unavailable] PORT_TAKEN  severity=informational  owner=runtime
    no live stacks this run: 127.0.0.1 could not serve on port 8080
    port 8080 is held by something that is not a stack server (Address already in use);
    pass --callstack-port with an unused port, or leave it off entirely and let one be drawn
    · the run itself is unaffected; what is missing is the live view
```

Two verdicts, because they have different remedies. `PORT_TAKEN` is a stranger
on the port — name another one. `BIND_REFUSED` is an address that is not an
interface on this machine, or a sandbox that forbids listening — naming another
port does not help.

Neither is raised when **another of your own sessions** holds the port: that is
the shared mode working as designed, and alerting on the ordinary case is how a
kind gets filtered out entirely. It is reported once per address per run, not
once per retry, though a named port held by a stranger is re-probed for the
life of the run.

`owner=runtime`, `severity=informational`: no test is at fault and nothing is
broken. What is lost is a diagnostic, and somebody has to decide whether to
reconfigure it.

### Who may ask

Every endpoint but `/identity` requires a token, minted per server and written
into the address file beside the port:

```console
$ TOKEN=$(jq -r .token .pytest-failures/*/callstack-*.json)
$ curl -H "Authorization: Bearer $TOKEN" localhost:8080/workers
$ curl "localhost:8080/workers?token=$TOKEN"    # for when you are in a hurry
```

That file is created readable by its owner alone — opened `0o600` rather than
written and then narrowed, because narrowing afterwards leaves a window and a
window is all anybody needs. So the boundary is the filesystem's: whoever can
read this run's evidence directory can read its stacks, and nobody else can.

**Loopback is not that boundary**, which is why the token is not conditional on
binding off it. Loopback bounds the reachable set to processes on this machine,
and *every user* on this machine is inside that set — so on a shared box, "only
local" and "only you" are very different statements, and only the second is
worth making about a service that reports what your test processes are
executing.

`/identity` is deliberately open: it is what one session asks another before
standing down from a contested port, and the two share no token. It answers
with a service name, a version and a pid, and never with the token.

A token lives and dies with the process that minted it, so one that leaks
expires with the run — that is the whole of its lifetime management.

### Finding the server

The run tells you, on a hook, the moment it is serving:

```python
def pytest_failure_server_ready(server):
    registry.upsert(
        session=server.session_id,       # names this run's evidence directory
        url=server.url,                  # already bracketed if the host is IPv6
        port=server.port,                # what got bound, never the 0 you asked for
        token=server.token,              # dies with the process that minted it
        pid=server.pid,                  # the controller, not any worker
    )
```

That is the whole address, and for a drawn port it is the only way to learn it
before the run is over — nobody can configure a number that did not exist a
moment ago. `server` is a `LiveStackServer`; `server.headers()` gives you the
`Authorization` header the endpoints want and `server.endpoint("/workers")`
joins the URL, so neither the scheme nor the slash is yours to get right.

The hook fires on a thread of its own once the server is already accepting, so
it is free to call straight back into the server it was just handed. It does not
fire at all when the server was never switched on, nor when this session stood
down because another of ours already holds a named port — that session announced
itself, and one server should not be stored twice.

**No run id in the payload.** At the moment the server binds, xdist has usually
not built its node manager, so this run's real id does not exist yet; stamping
the placeholder onto a row you will join against later is a key that silently
matches nothing. `session_id` is stable from the first moment, and `/workers`
reports the run id per directory as soon as a worker beats.

If you would rather poll the filesystem than implement a hook, the address is
also on disk. A drawn port is written to `callstack-<pid>.json` in **this run's**
evidence directory (see the layout below), one file per serving session, and
removed when that session ends. Files left by a
session that was killed are swept by whoever publishes next — by checking the pid
in the filename, so a live session's address is never deleted.

```python
for address in Path(".pytest-failures").glob("*/callstack-*.json"):
    run = address.parent                      # one directory per run
    server = json.loads(address.read_text())["url"]
    for state in run.glob("*.state"):
        record = json.loads(state.read_bytes().rstrip(b"\x00").strip())
        stack = requests.get(f"{server}/stack?pid={record['pid']}").json()
        print(run.name, record["nodeid"], record["phase"], stack["threads"][0]["frames"][0])
```

```
test_pool.py::test_concurrent_writes call {'function': '_wait_for_lease', ...}
```

### Pushing samples instead of polling for them

`/workers` and `/stack` are a pull: something outside asks, when it wants to
know. For a dashboard watching one run that is exactly right. For a fleet it is
not — a poller has to reach every host, and the answer for almost every worker
is "still working", which the heartbeat already said.

`failure_sample_seconds` turns the same information around and pushes it:

```python
def pytest_failure_worker_sample(sample):
    for worker in sample.workers:
        rows.insert(session=sample.session_id, at=sample.observed_at,
                    worker=worker.worker, nodeid=worker.nodeid,
                    status=worker.status, rss_mb=worker.rss_mb,
                    cpu_rate=worker.cpu_rate, digest=worker.stack_digest)
        if worker.stack:                     # only when it is news
            frames.insert(digest=worker.stack_digest, frames=worker.stack)
```

Off by default. It is the only hook here that fires when nothing is wrong, so
it is the only one with a running cost — and two decisions keep that cost off
the floor:

**Only stuck workers are read.** Every worker's status comes from files the run
was writing anyway, with nothing asked of the worker itself, so the rows are
nearly free. Frames are taken only for `blocked` and `frozen`. A worker burning
CPU is working, and reading its stack costs a subprocess and a pause to learn
what the heartbeat already reported. `unmeasured` is not sampled either: it is
what every worker looks like with the watchdog off, so sampling it would quietly
mean sampling everything.

**An unchanged stack is sent once.** The workers worth watching are the ones
whose frames are *not* moving, so the same stack is drawn over and over. It goes
out once; after that `stack` is `None` while `stack_digest` and `stack_repeats`
say which stack it still is and for how long. A worker wedged for a day costs
one stack and a counter rather than thousands of copies of one fact.

The difference is not marginal. Reading every worker every ten seconds across a
few thousand concurrent workers is hundreds of gigabytes a day; the same period
sampled this way is single-digit gigabytes, and the stack trail for anything
that actually got stuck is identical.

Set `failure_sample_stacks = false` to keep the rows and decline the frames —
the two halves have very different prices and are worth being able to buy
separately.

### Containers

`--callstack-host 0.0.0.0` is what a container needs: its UI is outside, and
127.0.0.1 inside a container is unreachable from there. Binding anything but
loopback warns, once — but the token below is what makes it survivable rather
than reckless.

Two things about containers make this easier than it looks:

- **Each container has its own network namespace**, so `8080` inside one pod is
  not `8080` inside another. The port contention that the named mode exists to
  resolve does not arise between pods at all — it is a bare-metal and laptop
  problem. Name a port in a container, publish it, and every pod can use the
  same number.
- **Each container has its own PID namespace**, so a server in one pod could not
  read another pod's workers even with every permission granted. Sharing is
  neither possible nor needed there.

Reading a process needs ptrace, which modern Docker permits under its default
seccomp profile. Where it is refused, the endpoint says so and names the fix
rather than returning nothing:

```json
{"pid": 48213, "source": "py-spy", "error": "Operation not permitted (os error 1)
 - ptrace is not permitted: check /proc/sys/kernel/yama/ptrace_scope (0 or 1 allows
 this; 1 requires the target to be a descendant of the reader, which xdist workers
 are), and add --cap-add=SYS_PTRACE if this is a container"}
```

`ptrace_scope` is a host-wide sysctl and is **not namespaced**, so a container
inherits the node's value and cannot change it. At `ptrace_scope=1` the
*tracer* must be an ancestor of what it reads — and the tracer is not the
controller but py-spy, which the controller spawns. py-spy and a worker are
both children of the controller, so they are siblings, and a sibling is not an
ancestor.

Every worker therefore nominates its parent as a permitted tracer at startup,
via `prctl(PR_SET_PTRACER, <controller pid>)` — the exception Yama provides for
exactly this. That covers whatever the controller spawns to do the reading and
nothing else on the machine; `PR_SET_PTRACER_ANY` would also work and is not
used, because it opens the process to every uid that could already ptrace.
`worker_start` records whether the exception was granted, so a refused read has
an answer beside it rather than only a message.

A *named* port shared across sessions still reads only the workers of the
session hosting it: another session's workers nominated *their* controller, not
this one. `failure_tracer = any` is what lifts that — it drops the relationship
requirement entirely, so any reader on the machine that could already ptrace is
permitted. That is the setting a shared server needs and the one a private run
does not, which is why it is not the default.

### Reading another process needs py-spy

`pip install pytest-failure-instrumentation[stacks]`. There is no way to walk
another process's frames from Python, so any pid but the server's own is read
externally. That is also what makes it work on a worker whose GIL is held by
native code: py-spy reads the target's memory rather than asking it to run
anything, and stops the target before reading, so it never walks a frame that is
being torn down. The server's *own* pid is answered from `sys._current_frames()`
— no subprocess, no ptrace, no permission.

Without py-spy the endpoint still answers, with the reason instead of a stack. A
UI that is told *why* it has no stack can tell a dead process from a missing
permission; one that gets an empty response cannot.

On Windows there is no ptrace and no equivalent restriction: any process can
read another running as the same user at the same integrity level, so the
descendant rule above simply does not apply. Reading an *elevated* process from
an unelevated one needs `SeDebugPrivilege`.

## One directory per run

```
.pytest-failures/
  run-70a514cc7a93/    <- this pytest process's own name for itself
    owner.json         <- the controller's pid, and the only thing that makes
    gw0.state             this directory ours to delete
    gw0.events         <- every line carries the *reported* run id
    gw1.state
    callstack-4213.json
```

Runs used to share a flat directory and name their files after the worker,
which works exactly until two runs happen at once — and on a laptop or a
bare-metal runner that is the ordinary case. Every worker is `gw0`, so the
second run's `gw0.state` *is* the first run's `gw0.state`: one run reads the
other's evidence, believes it, and attributes a stall to a test a different run
is running. The old start-of-run cleanup made it worse rather than better,
because it deleted the files of a run still using them.

A directory per run removes the class of bug rather than a symptom. Nothing
inside is named for the run, because the directory already is, so every path a
reader builds is unchanged.

**Why the directory is not named after the run id you see on incidents.** The
obvious name is xdist's own run id, and it cannot be used: it does not exist
until xdist has built its node manager, and there is no hook order that
reliably puts that first. `trylast` does not do it, because xdist's own session
start is *also* `trylast` — so which of the two runs first comes down to which
plugin registered first, and that differs between installing from the entry
point and installing from a framework's `pytest_configure`. The directory is
therefore named by something this process fixes for itself and nothing can
reorder. The reported run id still prefers xdist's, so incidents still line up
with xdist's logs, and every `.events` line inside carries it — which is how a
directory is matched back to a run.

`PYTEST_RUN_ID` names the directory if you set it, which is also the way to
make two runs deliberately share one.

**What gets cleaned up.** Whole directories of runs that are *over* — not old.
The controller's pid is in `owner.json`, so a run still going is recognisable
as such however long it has been going, which matters precisely because several
run at once. A directory without that marker is not ours and is left alone
whatever it looks like, which includes the flat files an older version of this
plugin left behind: they cannot be mistaken for a current run's evidence,
because a current run does not look there for any.

## Cost

A passing test must cost as close to nothing as possible, because that is the
overwhelming majority of what runs.

- Per test: six fixed-size writes to a file that never grows — two per phase,
  one as it opens and one as it closes, which is what separates "died in
  teardown" from "died mid-call" — plus two clock reads. No append log, no
  `/proc` read, no allocation tracking.
- Per test that outlives `failure_slow_test_seconds` (measured setup through
  teardown): one ~5 KB stack dump every interval, written and renamed by the
  heartbeat thread, and an unlink when the test ends. Nothing accumulates
  across tests, and nothing is written for a test that finishes in time.
- Per 5 seconds, per worker: one heartbeat carrying CPU time and resident
  memory. Per second, per worker: two deadline comparisons and a timer rearm,
  which is why the first stack of a wedged test does not wait for a beat.
- Off by default: `tracemalloc` (needed to attribute an OOM kill to a source
  line) and the live-object census — walking the heap on a worker near its
  ceiling is exactly the instrumentation that makes things worse.
- The live stack server, when switched on: one thread per session, which
  either serves or retries the claim every five seconds. Nothing is sampled
  and nothing is written unless something asks.
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
| `failure_directory` | `.pytest-failures` | Where evidence is written; each run gets a subdirectory under it |
| `failure_watchdog` | `true` | Memory and liveness sampling |
| `failure_heartbeat_interval` | `5.0` | Seconds between liveness beats (floor 1.0) |
| `failure_tracemalloc_depth` | `0` | 1 names the allocating line for OOM attribution |
| `failure_object_census` | `false` | Count live objects at a high-water mark |
| `failure_high_water_mb` | auto | Memory mark for a snapshot; defaults to a share of the discovered limit |
| `failure_memory_limit_mb` | `0` | Soft cap (POSIX) turning an OOM kill into a `MemoryError` |
| `failure_slow_test_seconds` | `20` | How often a running test refreshes its stack (setup through teardown; needs `failure_watchdog`) |
| `failure_stall_seconds` | `300` | Silence before a stall is assessed |
| `failure_stack_probe` | `true` | Ask a diagnosed stalled worker for a fresh stack (POSIX) |
| `failure_tracer` | `parent` | Who may read a worker on Linux under Yama: `parent`, `any`, `off` |
| `failure_sample_seconds` | `0` | Push a worker sample this often while the run is going. 0 is off |
| `failure_sample_stacks` | `true` | Whether those samples carry frames for workers that look stuck |
| `failure_stack_server` | `false` | Serve live stacks over HTTP |
| `failure_stack_server_port` | `0` | 0 draws a free port and writes it down; any other is claimed and shared (`--callstack-port`) |
| `failure_stack_server_host` | `127.0.0.1` | What it binds; `0.0.0.0` for a container (`--callstack-host`) |

`failure_slow_test_seconds` and `failure_stall_seconds` are not independent.
The stack a stalled worker is reported with is whatever the watchdog last
wrote, so the cadence has to have fired before the stall is assessed — a stall
judged sooner is judged with no stack at all, and on Windows that is every
stall. Neither is clamped, but an inverted pair warns.

`failure_memory_limit_mb` is worth a note: an `RLIMIT_AS` cap makes the
allocation fail *inside* the process, so you get a `MemoryError` with a
traceback and a node id instead of an uncatchable kill with neither. It costs
you a hard ceiling per worker, which is why it is opt-in.

`failure_directory` should not be shared by two runs going at once. Worker ids
start at `gw0` in every run, so two concurrent runs pointed at one directory
write the same file names — and the controller clears the directory of its own
files at startup, which is the other run's evidence as well. Everything either
run reads is stamped with the run that wrote it and a record naming a different
run is refused, so the failure mode is *missing* evidence rather than another
run's attributed to yours; but missing is still missing. The default is
relative to the rootdir, which separates two suites run from different
directories. Point it somewhere per-run — a build id, a matrix cell — if you
are collecting into a shared artifacts directory.

## Platform coverage

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| Test in flight, phase, exit status | yes | yes | yes |
| Crash stack | yes | yes | yes |
| Stack from a *slow or hung* test | yes | yes | yes |
| Current memory | procfs | psutil | psapi |
| Container limit, OOM counter | yes | n/a | n/a — no OOM killer |
| On-demand stack from a stalled worker | yes | yes | no |
| Live stack of another process (needs py-spy) | yes | root only | yes |
| Stack from a worker that stopped running Python | yes | yes | yes |

The last row is the frozen-interpreter fallback, and it is the one capability a
*setting* takes away on every platform rather than a platform taking away: where
`faulthandler_timeout` is set, pytest owns the single
`dump_traceback_later` timer it would arm, and it stands down rather than
cancel a timeout somebody configured. On Windows that leaves a worker frozen in
native code with no stack at all, since the on-demand probe is not available
there either — which is why the worker records `frozen_fallback_stood_down` in
its event log rather than leaving the absence to be guessed at.

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

`psutil` is a dependency, imported like any other. It is the only
cross-platform way to ask whether a process is still there, and the POSIX way
is actively dangerous on Windows: `os.kill(pid, 0)` sends a console event only
for `CTRL_C_EVENT` and `CTRL_BREAK_EVENT`, and calls `TerminateProcess` for
every other value — including zero. A liveness check written the obvious way
would kill each worker it inspected, and the live view inspects every worker on
every request. psutil also carries the memory figures on macOS and Windows,
which procfs cannot.

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

- **without `pytest-xdist`**, where `pytest_testnodedown` has no hookspec at
  all and an unspecced hookimpl is a registration error — the failure mode that
  once made a plain `pytest` run report nothing.
- **against the declared minimums**, `pytest==7.0.1` on Python 3.9. Every other
  job installs whatever is newest, so a hook signature or an ini type that
  arrived later would pass all of them and fail on a user's pinned pytest.

`ruff` and `mypy` run as their own job, and fail first because they are cheap.
The source carries `# noqa` and `# type: ignore` markers, which are only worth
writing if something reads them.

Two of the tests are about the plugin rather than about a failure: a run whose
evidence directory cannot be created has to keep running, and a directory
shared with somebody else's artifacts has to come out of a run with those
artifacts still in it. A reporting tool that ends a run, or eats a file, has
cost more than the failure it came to explain.

## Releasing

Tag the commit and the rest runs itself:

```console
git tag v0.2.0 && git push origin v0.2.0
```

The tag is the only input. `.github/workflows/release.yml` builds the sdist and
wheel, refuses to continue if the tag disagrees with the version in
`pyproject.toml`, installs the **built wheel** on Linux, macOS and Windows and
runs the whole suite against it, publishes to PyPI, and then creates the GitHub
release with the artifacts attached.

The wheel is tested rather than the checkout because this plugin is one entry
point. If packaging drops it the import still succeeds, the suite still passes,
and nothing is instrumented at all — the one failure mode a green test run
cannot rule out. So the release explicitly asserts the entry point exists and
that the package under test came from `site-packages`.

### Credentials

There is no API token to create and no secret to add to the repository.
Publishing uses [trusted publishing](https://docs.pypi.org/trusted-publishers/):
PyPI verifies this workflow's OIDC identity at upload time, so nothing
long-lived exists to leak or rotate. `GITHUB_TOKEN` is supplied by Actions
automatically.

What it does need is configuration, once, on each side.

**On PyPI** — *Your account → Publishing*. The project does not exist there
yet, so this is an **"Add a new pending publisher"**, not a setting on an
existing project; a pending publisher is how a first upload is authorised for a
name nobody has claimed. It becomes a normal publisher after that first
release.

| Field | Value |
|---|---|
| PyPI project name | `pytest-failure-instrumentation` |
| Owner | `Heknon` |
| Repository name | `pytest-failure-instrumentation` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

**On GitHub** — *Settings → Environments → New environment*, named `pypi`.
Under it, tick **Required reviewers** and add yourself. That is the manual gate:
the run pauses before anything reaches PyPI, shows you the tag it is about to
publish, and waits. Nothing is uploaded until someone approves, and waiting does
not consume the job's timeout.

Worth setting at the same time, under *Deployment branches and tags*: restrict
the environment to the tag pattern `v*`, so the only thing that can ever reach
PyPI is a tagged commit.

**TestPyPI** is a separate site with a separate account, so rehearsing needs its
own pending publisher at test.pypi.org with the environment named `testpypi`.
Leave that environment without reviewers — the point of a rehearsal is that it
does not need one.

## Licence

MIT — see [LICENSE](LICENSE). Declared as an SPDX expression under
[PEP 639](https://peps.python.org/pep-0639/) rather than a classifier, since
PyPI rejects a distribution carrying both.

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
