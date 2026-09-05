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
Worker gw1 crashed with SIGSEGV (segmentation fault in native code) while running test_crashes.py::test_crashes (call), in native_call (engine.py:6)   [worker_death NATIVE_CRASH, product, critical]
    Exit status -11 read via waitid (pid 805).
    The worker wrote a stack as it died.
    Look at: test_crashes.py::test_crashes.
    Measured: 1 test started and 0 finished on this worker.
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
distinct code for "out of memory". A cgroup counter increase shows that some
process in the group was OOM-killed; it does not identify this worker.
`OOM_KILLED` requires a kernel record naming the victim in the death window.
Without that evidence, the incident retains `SIGKILLED` and reports the counter
as context. Direct signal evidence takes precedence over that correlation.

### The failures that reach no hook at all

A worker's death at least fires a hook. Four others do not.

**A run that stalls.** `pytest_testnodedown` needs a dead process; a wedged
one is alive. And the controller hears from a worker only when a phase
*completes*, so from outside, a twenty-minute test and a deadlock are the same
event: nothing. The run does not fail — it never ends, and CI kills the job an
hour later with no artifact naming a test.

Without xdist there is no outside at all, which sounds like the harder case and
is the easier one: a main thread blocked on a lock or a socket does not stop
the other threads in its process, so the run can be asked what it is doing by a
thread of its own. A plain `pytest` that deadlocks now names the test, prints
the stack of the thread that is stuck, and says so *while it is still stuck*.

**Workers that collected different tests.** xdist notices, writes a unified
diff per differing worker into its own log, and aborts. Nothing structured
reaches a hook. With sixty workers and one odd node that is fifty-nine complete
diffs, every one of them naming the majority as the deviation.

**A run that never came back.** Every failure above is reported by a process
that survived to report it. A run that was *itself* killed has no survivor: a
plain `pytest` that segfaults, a controller reclaimed along with its workers, a
CI job cancelled mid-suite. Nothing fires, nothing is written, and the only
trace is a job that stopped. What that run recorded is on disk and complete —
so it is reported by the next run over the same directory, which was already
walking it to clear it out.

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
Worker gw1 was killed with SIGKILL before running any test   [worker_death SIGKILLED, unknown, needs-triage]
    Exit status -9 read via waitid (pid 21780).
    No cgroup OOM kill was counted during this run. SIGKILL cannot be caught, and is sent by a host OOM killer, a container or CI cancellation, or a kill command.
    Measured: 31 MB resident at the last heartbeat. 0 tests started and 0 finished on this worker.

pytest hit an internal error: KeyError: <WorkerController gw1>, in _assign_work_unit (loadscope.py:275)   [internal_error INTERNAL_ERROR, runtime, high, run-ending]
    Raised on the controller and captured first-hand.
    pytest ends the session on an internal error.
    Look at: the full traceback, kept in the incident's detail field.
    Severity high rather than informational: a defect in the framework ended the run, and no test is at fault, so nothing else reports it.
```

The `runtime` owner in the tag is the load-bearing part. No test is at fault
and no worker is at fault, so nothing else in the run will ever surface this —
which is exactly why it is the one case where a framework defect is raised *above*
informational.

## Install

```console
pip install pytest-failure-instrumentation
```

Installed is not switched on. The package registers a `pytest11` entry point
so that its hooks and its `failure_*` options exist on every run, and then does
nothing until a run asks for it:

```console
pytest --failure-instrumentation
```

Put the switch in `addopts` to have every run ask, and tell it which packages
are yours, so a failing frame in your code can be told from one in a dependency
or in the customer's own tests:

```ini
[pytest]
addopts = --failure-instrumentation
failure_packages = yourcore, yourcore_ext
failure_product_version = 4.2.0
```

Implement one hook to receive what it finds:

```python
# conftest.py, or your own plugin
def pytest_failure_incident(incident):
    database.save(incident.model_dump())
    alerts.send(str(incident))
```

Without the hook it still writes its evidence to `.pytest-failures/`.

Naming the live view switches it on as well: `--callstack-port` or
`--callstack-host` is a request for a server that cannot run without the
plugin under it, and an option accepted and then ignored for want of a second
one is worse than either behaviour on its own. The other way to switch it on is
to call `install` from your own code, below. `-p no:failure_instrumentation`
removes the entry point altogether, options and hookspecs included.

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

**Turning the entry point off.** `-p no:failure_instrumentation` skips the
entry point entirely; `install` puts back the hookspecs so
`pytest_failure_incident`, `pytest_failure_worker_sample` and
`pytest_failure_server_ready` all still reach their implementers. Note that it also skips `pytest_addoption`, so
`failure_*` ini keys become unknown config options — which is the point if your
framework owns the settings, and a reason to leave the entry point enabled and
just call `install` if it does not. Either way `install` is itself the switch:
a framework that calls it has asked, and nobody has to pass
`--failure-instrumentation` as well.

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
| `worker_death` | `WorkerDeathIncident` | any run |
| `worker_stall` | `WorkerStallIncident` | any run |
| `collection_mismatch` | `CollectionMismatchIncident` | needs xdist |
| `internal_error` | `InternalErrorIncident` | any run |
| `run_summary` | `RunSummaryIncident` | any run |

Only the two that are *about* workers need workers. Everything else is raised
whether or not you run under xdist, because the process that records is
whichever one runs the tests — under xdist a worker, and without it the session
itself.

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

### How an incident reads

`str(incident)` is the alert text, and every kind renders to one shape. It is
a convention rather than a style, and `tests/test_message_convention.py`
holds every kind to it:

1. **The first line says what happened, in words**, specific to this instance:
   which worker, which signal, which test, which function. It ends with a tag
   `[kind VERDICT, owner, severity]` to grep for and route on, with
   `run-ending` appended when the session died with it. A `run_summary` has
   no owner slot, because nothing failed.
2. **If no stack named anybody**, one line says where the owner came from:
   `No stack was captured; the owner is taken from the test that was running,
   test_pool.py (customer-code).` A lead, marked as one.
3. **Every other line is exactly one of three things.** A *measurement*: what
   was observed, and where the figure came from when that matters (`Exit
   status -11 read via waitid`). What that measurement *means by
   construction*: what follows from how it was taken, or from a fact about
   the OS, the runtime or xdist (`SIGKILL cannot be caught`, `xdist addresses
   tests by position`). Or a *place to look*: `Look at:` a file and line, a
   test, a setting, a flag. Several numbers share one `Measured:` line at the
   end, labelled.

What is deliberately absent: any guess at a cause in your code, and any fix
to it. The analysis is arithmetic over evidence; it knows that a thread other
than the test's used the CPU, not that the thread is a poller you forgot
about. A line that named the poller would be right often enough to be
trusted and wrong often enough to matter. Sentences are short, start with a
capital and end with a full stop; nothing is a field dump, nothing argues for
the finding, and nothing is said twice.

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
| `POSSIBLE_TIMEOUT` | a self-exit or SIGALRM that reached its effective terminating deadline. Medium-confidence correlation, not proof |
| `OOM_KILLED` | `-9` **and** the kernel log names this pid as the OOM killer's victim, within the death window |
| `KILLED_BY_PROCESS` | `-9`, and the kernel's signal tracepoint saw a process outside the run send it — the sender is named |
| `KILLED_BY_RUN` | `-9` sent by another process of this run: the controller (execnet terminating a worker that would not exit) or a sibling worker |
| `SELF_KILLED` | `-9` the worker sent to itself |
| `KILLED_BY_KERNEL` | `-9` with `si_code` `SI_KERNEL` — the kernel's own kill, and no readable log to say which |
| `KILLED_AFTER_SIGTERM` | `-9`, and the controller had been sent SIGTERM shortly before — the run was being stopped, and the sender is named |
| `SIGKILLED` | `-9` and no witness answered; the incident says which witnesses this machine withheld, and why |
| `NATIVE_CRASH` | SIGSEGV/SIGABRT/SIGBUS/SIGILL/SIGFPE, or a Windows NTSTATUS |
| `SIGNAL_<n>` | SIGTERM/SIGINT/SIGHUP — a request to stop, not a defect |
| `SELF_EXIT` | any exit code with no signal, `0` included — a worker that left the run was not asked to |
| `PROBABLY_SIGNALLED` | exit code 128–191, a wrapper ate the signal |
| `RUN_STOPPED` | a run found dead afterwards whose controller had been sent SIGTERM before this process's last heartbeat |
| `UNKNOWN` | no status obtainable (remote gateway) |

See [Who killed it](#who-killed-it) for where each of those witnesses comes
from and what it needs.

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

In a run with no workers the assessment is the same assessment, from the same
files, made by a thread inside the process it is about. The stack is read the
way every live process here is read — py-spy, from outside — even though this
one's frames are also directly to hand: a second reader for the single case
that could avoid the first is a second set of failure modes and a second source
to explain. It reads memory rather than asking the process to run anything, so
no signal is sent that could return a blocked syscall early and dissolve the
stall, and Windows — where no process can be *asked* for a stack — gets a
current one like everywhere else. Where py-spy cannot read the process — macOS
without root — the verdict is unchanged, since it comes from beats rather than
frames, and the stack is whatever the watchdog last wrote.

The exception is `STALLED_FROZEN`, which is precisely the case where no Python
runs: the watcher thread cannot run either, so a frozen lone run reports
nothing until somebody reads what its fallback timer left behind.

### Workers collected different tests

| Verdict | Means |
|---|---|
| `COLLECTION_MEMBERSHIP_DIFFERS` | a test exists on one machine and not another |
| `COLLECTION_ORDER_DIFFERS` | same tests, different sequence — fatal too, since xdist addresses tests by position |
| `COLLECTION_PARAMETERS_UNSTABLE` | same tests, different *parameter values* — a parametrize that is not deterministic |

Sixty workers never produce sixty collections. They produce two or three
*variants*, so this reports one row per variant, measured against the largest:

```
Workers collected different tests: 2 workers produced 2 different collections   [collection_mismatch COLLECTION_MEMBERSHIP_DIFFERS, unknown, needs-triage, run-ending]
    No stack was captured; the owner is taken from a module the workers disagreed about, test_collect.py (customer-code).
    Baseline: 1 worker collected 3 tests; the rows below are measured against that list.
    1 worker is missing 1 test, in test_collect.py (gw1).
        - test_collect.py::test_two
    xdist addresses tests by position rather than by id, so any difference between the lists stops it: a reordering as much as a missing test.
    The initial collections disagreed, so xdist aborted the run.
    Look at: the full id lists in .pytest-failures/run-3f9a1c2d.
```

At sixty workers it stays the same shape, because the row count follows the
number of *variants* rather than the number of workers:

```
Workers collected different tests: 58 workers produced 3 different collections   [collection_mismatch COLLECTION_MEMBERSHIP_DIFFERS, unknown, needs-triage, run-ending]
    Baseline: 55 workers collected 300 tests; the rows below are measured against that list.
    2 workers are missing 1 test, in test_payments.py (gw41, gw58).
        - test_payments.py::test_case_017
    1 worker has 6 extra tests, in test_legacy.py (gw17).
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
Workers collected the same tests with different parameter values: 6 workers produced 6 different collections   [collection_mismatch COLLECTION_PARAMETERS_UNSTABLE, unknown, needs-triage, run-ending]
    The same tests exist on every worker; only the parameter values in their ids differ.
        test_billing.py::test_invoice
            gw0 collected acct-1791, acct-3471, acct-6305, acct-7468
            gw1 collected acct-2186, acct-2542, acct-6991, acct-9779
            gw2 collected acct-1614, acct-1950, acct-4517, acct-9313
    Ids that differ per worker were computed at collection time from something that differs per process, and xdist requires the ids to match.
    Look at: the parametrize arguments of those tests.
    xdist addresses tests by position rather than by id, so any difference between the lists stops it: a reordering as much as a missing test.
    Stripping the parameters from the ids makes every worker's collection identical.
    The initial collections disagreed, so xdist aborted the run.
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

**Whichever process runs the tests is the one that records.** Everything below
is written from inside that process, because a process that is about to be
killed gets no warning and nothing it knew only in memory survives. Under
xdist that process is a worker and the controller reads what it left. Without
xdist there is one process and it does both jobs — so a plain `pytest` writes
the same state slot, the same heartbeat and the same stacks a worker does,
under the name `main`, and the live view, the sampler and the stall watcher
read them the same way.

The one thing it does not take, unless asked, is the fatal dump.
`faulthandler` keeps exactly one destination for a fatal signal and pytest's
own plugin has already pointed it at stderr; a worker claims it and loses
nothing, because that stderr is shared with fifteen others and a dump written
into it belongs to nobody. A run with no workers would be taking the crash out
of a terminal somebody is watching, so it leaves it there — see
`failure_crash_stack`, which is that trade written down.

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

**And a run that never came back is read by the next one.** The corpse being a
whole run rather than one worker changes only who does the reading. A starting
run already walks the evidence directory to clear out the runs that are over;
the same walk now asks a second question of the same marker, and two answers
separate a run to report from a run to delete:

- *Is it over?* The owner's pid is in the marker, so a run still going is
  recognisable however long it has been going — which matters because several
  run at once, and deleting a live run's evidence is how a cleanup once broke
  the very reports it existed to produce.
- *Did it report for itself?* A run that reaches session finish stamps the
  marker on the way out. It raised its own incidents; re-raising them a day
  later against whichever run happened to notice is worse than never raising
  them.

A run that is over and never stamped that mark is exactly a run that could not
report for itself, and each process in it that started a session and never
finished one is a death:

```
Worker main of run run-8f21c0b4e5d7 died while running test_pool.py::test_writes (call); its exit status could not be read   [worker_death UNKNOWN, unknown, needs-triage]
    No stack was captured; the owner is taken from the test that was running, test_pool.py (customer-code).
    Found in the evidence of run run-8f21c0b4e5d7, which ended without reaching session finish; it was last seen alive at 2026-09-04 09:12:41. The death is somewhere after that.
    Exit status unavailable (pid 21780): only the parent process could read it, and the parent was the run that died. An OOM kill, a segfault and an os._exit cannot be told apart without it.
    Look at: test_pool.py::test_writes.
    No stack was kept: this run had no workers, so its fatal dump went to the terminal that pytest's faulthandler plugin writes to.
    Look at: failure_crash_stack, which keeps a copy in the evidence directory.
    Measured: 412 MB resident at the last heartbeat. 12 tests started and 11 finished on this worker.
```

`recovered_from_run` leads the block, because a reader who takes this for the
current run's crash goes looking for a failure that is not there. `run_id` is
the dead run's — that is the key anything joins on, and the run that merely
found it had no part in what happened. `raised_at` is now.

The exit status is the one thing genuinely lost. Only a parent may read it, the
parent was the run that died, and the process is long gone — so `-9`, `-11` and
`os._exit(1)` cannot be told apart afterwards, and the incident says that
rather than guessing. Everything else survives: which test was in flight and in
which phase, how many had run, the resident memory at the last beat, and — if
the run kept one — the fatal stack, which is enough to reach `NATIVE_CRASH`
with a blamed frame and no status at all. That is the same position Windows is
in for a watched worker, and it is why the dump is evidence in its own right.

## A worker that timed out

The worker saves each test's effective timeout settings, including marker
overrides, disabled timeouts and call-only scope. Call-only deadlines use the
call clock; other deadlines use the test clock. A plain `faulthandler_timeout`
only dumps stacks and is excluded unless pytest also enables
`faulthandler_exit_on_timeout`.

A self-exit or SIGALRM after an effective deadline is `POSSIBLE_TIMEOUT` with
medium confidence. Timing is consistent with an enforcer, but does not prove
that it caused the exit. An explicit exit can occur after the same deadline.
`matched_timeout`, `timeout_source`, `test_seconds` and `phase_seconds` expose
the comparison. Marker values replace the global limit, including `timeout(0)`;
a test with a raised limit is not judged against the smaller global value.
Older evidence without effective settings is left unclassified as a timeout.

## What the worker last said

The line that explains a native death is usually on stderr and nowhere a stack
can reach it: `OpenBLAS blas_thread_init: pthread_create failed` when a hundred
workers each start a thread pool at once, a `malloc(): corrupted top size`, a
library's own abort message. pytest already captures it — its fd-level capture
catches even the writes C code makes straight to file descriptor 2 — but it
keeps it in a temporary file that the kill throws away, and hands it to the
report only for a phase that *completed*.

With `failure_capture_output` on, fd 2 is pointed at a real file for the length
of each phase, and the last few kilobytes reach the incident as `recent_output`,
with the final stderr line surfaced on the alert:

```
    · segmentation fault in native code
    · last stderr: OpenBLAS blas_thread_init: pthread_create failed
```

A real file rather than a pipe on purpose: a write followed immediately by
`abort()` is a synchronous `write(2)` the kernel has persisted before the abort
runs, where a pipe drained by a thread of the same dying process would lose the
race. So the message printed in the very phase that crashes is kept — the case
pytest's own capture never reports, because it hands a phase's output to a
report only once the phase has completed.

It coexists with pytest rather than fighting it. pytest owns fd 2 by pointing
it at its own file and re-points it there at the start of every phase and every
import; this takes it over just after, at each, and hands the phase's bytes
back to pytest's file at the phase's end, before pytest reads it — so pytest's
captured-output-on-failure is unchanged. It is the one facility here that takes
over a process-wide descriptor, which is why it is opt-in and guarded at every
step: an fd operation that fails leaves fd 2 as it was and records that output
was not captured, rather than raising. It stands down for a test that captures
its own fd output — one that asks for the `capfd` or `capfdbinary` fixture —
because taking fd 2 out from under such a fixture would make its `readouterr()`
miss what the test wrote, and changing a passing test is the one thing this may
never do; `capsys` and `capsysbinary` are sys-level and untouched. Tested
alongside `-s`, `--capture=sys` and pytest-cov, each of which keeps its own
output and its coverage while a crash is still caught. POSIX only. An absent
tail reads as "not captured", never as silence: the alert says which.

## Who killed it

A wait status of `-9` is the one number designed to say nothing about who sent
the signal, and for a long time this plugin stopped there: `SIGKILLED`, with a
list of what it might have been. Everything that actually ends a process keeps
a record somewhere else, so it now goes and asks each of them — and where a
machine refuses, the incident says which witness was withheld and by what,
rather than leaving an absence to be read as "nothing to know".

**The kernel's signal tracepoint names the sender of every SIGKILL.**
`signal:signal_generate` fires in the *sender's* context when a signal is
queued, so its line carries the sender's comm and pid in front and the target
behind, with `si_code` — `0` for a `kill(2)` from a process, `128` for the
kernel's own, the OOM killer included:

```
python-1771  [000] d..1.  401.375501: signal_generate: sig=9 errno=0 code=0 comm=sleep pid=1772 grp=1 res=0
```

That is the difference between "SIGKILL, could be anything" and:

```
[worker_death] KILLED_BY_PROCESS  severity=informational  owner=unknown
    worker=gw1  in flight test_sleep.py::test_sleeps  phase=call  started=1 finished=0
    · died while running test_sleep.py::test_sleeps (call)
    · exit status -9 - SIGKILL: uncatchable kill (OOM killer or external kill) (pid 2011, via waitid)
    · SIGKILL was sent by gitlab-runner (pid 2003, uid 998), outside this run - `gitlab-runner run`: something outside this run stopped it - a job cancellation, a timeout enforcer, an orchestrator, or a hand on the keyboard
```

No test is suspected, because no test did it; the severity is informational,
because a cancellation is not a defect; and the sender's command line is on
the incident for whoever wants to take it up with them. A worker that sent
the signal to itself is `SELF_KILLED`; one killed by the controller — execnet
terminating a worker that did not exit in time — or by a sibling worker is
`KILLED_BY_RUN`, and those two *do* point at a test.

Reading tracepoints needs root, so a **sidecar** does it: a second
interpreter running a stdlib-only script, started directly where the run is
already root and through `sudo -n` where it is not and `failure_elevate`
allows it (`-n`: a sudo that would prompt fails rather than hangs). It makes a
tracefs *instance* of its own under `/sys/kernel/tracing/instances/`, enables
one event in it filtered to SIGKILL and SIGTERM, writes one JSON line per event
into `signals.log` in the run's directory — stamped with the wall clock as it
reads the pipe, and with the sender's command line read out of `/proc` in the
same instant, because the sender of a `kill -9` is usually gone a moment later
— and removes the instance on the way out. Nobody's `perf` or `trace-cmd` on
the same machine is touched.

**The kernel log names the OOM killer's victim, and prints the whole fleet.**
The cgroup counter says *that* something in the cgroup was OOM-killed; the log
says *what*. One `Out of memory: Killed process 4242 (python3) ... anon-rss:...`
line per kill, an `oom-kill:constraint=CONSTRAINT_MEMCG,...,task_memcg=/docker/...,pid=4242`
summary saying whether it was a cgroup's limit or the machine's, and — with
`vm.oom_dump_tasks` on, which is the default — a table of every task the
killer weighed with its RSS. For a run of a hundred workers that table is the
fleet at the instant the decision was made, and the incident does the
arithmetic:

```
    · the kernel log (kmsg) records the OOM killer choosing pid 4242 (python3) at 1680 MB anonymous resident, having hit the machine's own memory; matched by pid
    · it weighed 214 tasks holding 31650 MB; 100 of them were this run's, holding 29800 MB together; the victim was the 3rd largest
    · largest: python3 pid 4240 [gw17] 1720 MB, python3 pid 4241 [gw3] 1700 MB, python3 pid 4242 [gw52] 1680 MB
    · fleet pressure: the victim was an ordinary member of the run (median 290 MB), so the run's 100 processes exceeded the limit together - fewer workers or more memory, not one test
```

`fleet pressure` against `its own weight` is the question a hundred-worker run
needs answered: the killer takes whichever process is marginally the largest at
that instant, so the victim's own size explains nothing on its own, and the
in-flight test it happened to be running is the wrong suspect. Where the
tracepoint was also watching, the record says in whose context the kernel made
the kill — the process whose allocation hit the limit, which is often not the
victim.

Reading the log is the per-distro part, and every rung is tried in order with
the one that answered recorded on the incident: `/dev/kmsg`, open to everyone
where `kernel.dmesg_restrict` is 0 and only to `CAP_SYSLOG` where it is 1
(Ubuntu since 20.04, Fedora); `journalctl -k`, for members of `adm` or
`systemd-journal`; `dmesg`; and with `failure_elevate`, `sudo -n dmesg`.
Inside a pid namespace, host PIDs may not match the worker's PID. A shared
cgroup and nearby timestamp cannot identify a victim, so that case remains
unknown unless a direct witness identifies it. Undelivered signal-generation
events are ignored, and a catchable SIGTERM by itself does not prove death.

**Signal delivery is preserved.** The plugin does not block SIGTERM, replace
handlers or alter inherited masks in either controllers or workers. Where
permitted, tracefs records signal senders without intercepting delivery.
Without kernel tracing, an unprivileged controller death can be recovered as
`UNKNOWN`, with the unavailable evidence stated explicitly. Historical
controller records remain readable.

Live worker deaths share cached trace parsing and kernel-log snapshots. Slow
kernel-log reads run off the controller thread, with a single short wait per
refresh across a death cascade. Snapshot age is reported; an empty snapshot
does not prove that no OOM kill occurred.

### Reporting a killed run from the sidecar

Every incident above is raised by a process that survived to raise it, and a
run whose controller is killed has none. The next run over the same evidence
directory recovers it — but on a runner with a fresh workspace per job there
is no next run, and a cancelled or OOM-killed job was a job about which
nothing was ever said.

A sidecar can survive the controller. On POSIX it starts in a separate session,
but it can still be killed with the container, cgroup or host. It holds the read end of a pipe only the
controller can write, so the controller dying — whatever killed it — is EOF
on that pipe; a controller that reaches session finish writes `stop` first.
EOF without it is a death, and the sidecar then starts a *reporter*: a child
that builds the same incidents the next run would have recovered, the
controller's own death above all, and calls the callable you configured with
each one, the way the hook would be:

```python
# ci/alerts.py
def report_killed_run(channel, incident):
    from ourcorp.alerting import Client        # loaded in the reporter, not at pickle time
    Client.from_env().post(channel, str(incident), payload=incident.model_dump())
```

```python
# conftest.py
import functools
from ci.alerts import report_killed_run
from pytest_failure_instrumentation import install

def pytest_configure(config):
    install(config, on_run_death=functools.partial(report_killed_run, "#ci-deaths"))
```

or, from ini, as a dotted path with no bound arguments:

```ini
[pytest]
failure_on_run_death = ci.alerts:report_killed_run
```

**The callable travels as a pickle, and that sets the rules.** A module-level
function, or a `functools.partial` of one with picklable bound arguments;
lambdas, closures and anything holding the pytest config will not pickle, and
the run says so at session start and proceeds without a reporter. What pickle
records is the function's module and name plus the bound arguments, so
anything the function imports inside its body — your SDK — is loaded only in
the reporter, which runs with the same interpreter, the run's import path and
working directory, and the run's environment, which is where an alerting token
lives. The environment travels down the two pipes and never touches disk.

**Nothing of yours runs as root.** The Linux sidecar may be root. It unpickles
nothing; it starts the reporter as the user that started the run, with that
user's groups and environment. Its output goes to `reporter.log` in the run's
directory, and nothing it does can reach a run that is already over. Once
reported, the run's marker is stamped so a later recovery skips it. Reporters,
recovery and pruning share an OS lock. Each successful callback is checkpointed;
a failed callback leaves the remaining evidence for a later attempt. Delivery
is **at least once**: consumers should deduplicate by run ID and fingerprint
because a kill between callback success and its checkpoint can replay it.
The reporter's five-minute deadline includes input delivery; an overdue child
is killed and reaped. A successful callback means it returned without raising,
not that an external service durably stored the incident.

**Watch-only reporting needs no privilege.** Where tracing is unavailable, the
sidecar still starts when a reporter is configured. It reports durable state
and explicitly leaves the sender unknown. If the sidecar also dies, recovery
requires a later run over the retained evidence directory. Kernel sources may
be unavailable even with elevated privileges; the incident names the sources
that answered and those that did not.

**Windows keeps the same record through ETW.** There are no signals; a kill
is one process calling `TerminateProcess` on another, with whatever exit code
the caller chose — `1` from `taskkill /F` and from the GitLab runner, `-1`
from .NET's `Process.Kill`, `15` from Python's `os.kill`. The kernel logs the
call through the `Microsoft-Windows-Kernel-Audit-API-Calls` provider: event 2
carries the target's pid and the code, and the event header carries the pid of
the process it was written in, which is the caller. A sidecar of the same shape
as the Linux one consumes a real-time session on that provider, sweeps the
sessions a killed sidecar would have left (a machine holds at most 64), and
writes the same JSON lines; the verdict is `KILLED_BY_PROCESS` with the
caller's executable, or `SELF_KILLED` and `KILLED_BY_RUN` as on Linux. An ETW
session needs administrator rights or membership of Performance Log Users, and
there is no `sudo` to elevate with — `failure_elevate` does nothing there. Two
things differ from Linux: there is no OOM killer, so the memory case never
produces a kill (allocation fails inside the process as a `MemoryError`, which
is reported normally); and there is no warning shot, GitLab's own docs say the
kill is simply sent twice, so the unprivileged controller witness has nothing
to catch and ETW is the only source. A `taskkill /T` takes the sidecar with
the tree, so the kill of the controller itself may go unrecorded there; the
workers' kills before it are on disk.

macOS is the one platform still without a witness: it can be asked whether a
process died of memory pressure through `kqueue`'s process filter, which is
not read yet.

## Live stacks over HTTP

Everything above is for reading afterwards. This is the other direction: a UI
watching a run, asking what a test is doing *while it is still doing it*.

```console
$ curl localhost:8080/stack?pid=48213
{"pid": 48213, "source": "py-spy", "captured_at": 1756142887.31,
 "options": {"native": false, "locals": false, "nonblocking": false},
 "threads": [{"thread_id": 8632442880, "thread_name": "MainThread",
              "os_thread_id": 48213, "owns_gil": true, "active": true,
              "frames": [{"function": "_wait_for_lease", "file": "/app/pool.py", "line": 91,
                          "module": null, "native": false, "locals": null},
                         {"function": "checkout", "file": "/app/pool.py", "line": 44,
                          "module": null, "native": false, "locals": null},
                         {"function": "test_concurrent_writes", "file": "/tests/test_pool.py",
                          "line": 210, "module": null, "native": false, "locals": null}]}]}
```

**Naming the process.** `?pid=` is what `/workers` reports and what a UI already
holds. `?worker=gw3` is what a *person* holds — somebody looking at a stalled
worker is asking about that worker, not about the test it happens to be on, and
resolving the name at the moment of the read closes the window where xdist
replaces it between two requests. The name is compared against a directory
listing and never joined onto one, and a worker whose process has exited
resolves to nothing rather than to its last pid — pids are reused, and reading
one afterwards means reading whatever the machine has since given that number
to. Name it one way or the other; both at once is refused, because they can
disagree and there is no right one to prefer.

**Asking for more than the frames.** Three options, each one py-spy flag, all
off unless switched on. A bare `?locals` is on: only an explicit `?locals=0` is
a no.

| option | what it adds | what it costs |
| --- | --- | --- |
| `?native` | frames from C, C++ and Cython extensions | needs the process paused, and a py-spy that can unwind |
| `?locals` | each frame's variables, rendered as text | the largest payload here, and the data a test is holding |
| `?nonblocking` | reads without pausing the target at all | accuracy, and `owns_gil`/`active`, which become `null` |

```console
$ curl 'localhost:8080/stack?worker=gw3&locals'
{"pid": 48219, "worker": "gw3", "source": "py-spy", "captured_at": 1756142889.02,
 "options": {"native": false, "locals": true, "nonblocking": false},
 "threads": [{"thread_name": "MainThread", "frames":
   [{"function": "_wait_for_lease", "file": "/app/pool.py", "line": 91,
     "module": null, "native": false,
     "locals": [{"name": "timeout", "repr": "30.0", "argument": true},
                {"name": "waited", "repr": "27.4", "argument": false}]}]}]}
```

**`options` on the way back is what was *applied*, not what was asked for**, and
any difference is a sentence in `notes`. `--native` and `--nonblocking` are
refused as a pair by py-spy, so asking for both drops native — `--nonblocking`
is a promise about the target, and honouring native instead would pause a
process somebody asked not to have paused. A py-spy that cannot unwind is
likewise a reason to return the Python frames plus a note rather than an error.
A caller that displayed its own request back to a user would be captioning
frames with a setting that did not produce them, so read the toggles back from
here.

```console
$ curl 'localhost:8080/stack?pid=48219&native&nonblocking'
{"options": {"native": false, "locals": false, "nonblocking": true},
 "notes": ["native frames need the process paused, and --nonblocking was asked
            for as well; py-spy refuses that pair, ..."], ...}
```

`locals` is `null` when they were not asked for and `[]` when the frame holds
none — a native frame has no Python variables, and answering `null` there would
read as "you did not ask". The variables are rendered by py-spy inside its own
process while the target is stopped, so what crosses the wire is text and no
`__repr__` of yours is executed to produce it. They are nonetheless the one
thing this server discloses that is the *data* a test is working on rather than
the shape of the code — a fixture's credentials, a customer record, a decrypted
payload — so a deployment that cannot have that leave the process turns them off
and still gets the frames:

```ini
[pytest]
failure_stack_server_locals = false
```

Nothing is redacted selectively. This package cannot tell a password from a
lease id, and a filter that pretended to would be worse than the honest switch.

Off by default — a plugin installed for crash reporting should not start
opening listening sockets on everybody who upgrades it:

```ini
[pytest]
failure_stack_server = true
```

```console
$ pytest --callstack-port 8080          # also switches it on
$ pytest --callstack-host 0.0.0.0       # so does this - and needs a token
$ PYTEST_CALLSTACK_TOKEN=$(openssl rand -hex 16) pytest --callstack-host 0.0.0.0
```

A token does *not* switch it on: "authenticate the server I am already
running" and "start a server" are different requests, and an exported
`PYTEST_CALLSTACK_TOKEN` in a shell profile must not open a socket on every
pytest run in that shell.

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
           "schedule": {"dist": "load", "collected": 812, "unassigned": 240, "settled": false},
   "workers": [
     {"worker": "gw0", "pid": 21615, "nodeid": "test_slow.py::test_alpha", "phase": "call",
      "status": "blocked", "why": "heartbeat 0.5s old but no CPU progress: the test thread is waiting on something",
      "process_exists": true, "heartbeat_age_s": 0.5, "cpu_rate": 0.001, "rss_mb": 32,
      "tests_finished": 51, "tests_running": 1, "tests_queued": 12, "tests_assigned": 64},
     {"worker": "gw1", "pid": 21618, "nodeid": "test_slow.py::test_beta", "phase": "call",
      "status": "gone", "why": "process 21618 no longer exists; last seen in call of test_slow.py::test_beta",
      "process_exists": false,
      "tests_finished": 12, "tests_running": 1, "tests_queued": 7, "tests_assigned": 20},
     {"worker": "gw2", "pid": 21621, "nodeid": "test_slow.py::test_gamma", "phase": "call",
      "status": "working", "why": "heartbeat 0.3s old, burning 1.00 cores", "cpu_rate": 1.0,
      "tests_finished": 48, "tests_running": 1, "tests_queued": 11, "tests_assigned": 60}]}]}
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

A run with no workers is described the same way, under the name `main`. It is
the case where the two halves of the view coincide: the process serving is the
process running the tests, so `controller.pid` and the worker's pid are the same
number — and `/stack` for it is read exactly as any other pid is.

```console
$ curl localhost:8080/workers
{"runs": [{"session": "run-8f21c0b4e5d7", "controller": {"pid": 4212, "alive": true},
   "workers": [{"worker": "main", "pid": 4212, "nodeid": "test_pool.py::test_writes",
                "phase": "call", "status": "blocked",
                "why": "heartbeat 0.4s old but no CPU progress: the test thread is waiting on something"}]}]}

$ curl 'localhost:8080/stack?pid=4212'
{"pid": 4212, "source": "py-spy", ...}
```

The status vocabulary is [`analysis/stall.py`](#how-it-knows)'s truth table, as
a live status rather than a post-hoc verdict:

| status | heartbeat | CPU | process |
|---|---|---|---|
| `working` | fresh | above 0.05 cores | exists |
| `blocked` | fresh | below that | exists |
| `frozen` | stale | — | exists |
| `gone` | — | — | absent |
| `unmeasured` | never any | — | — |
| `finished` | stopped with the session | — | idle until the run ends |

The last row is the one that is not a finding, and it exists because of what a
worker does when it runs out of work: **it does not exit.** xdist sends it
`shutdown` once the queue is empty, which ends its test loop and nothing else.
The worker runs its own session finish, reports `workerfinished`, and its
process then sits inside execnet — main thread parked in
`integrate_as_primary_thread` on an `Event.wait` — until the *controller's*
session finish tears every gateway down at once. Under `--dist load` that is
however long the slowest worker's remaining tests take. Its heartbeat stopped
with its session, so from the beats alone it is a live process that has not
beaten for a while, which is the `frozen` row and its wording about native code
holding the GIL. The worker writes `worker_finish` into its event log on the
way out, and that record outranks the beats — and outranks `gone` too, since
being closed at the end of the run is how a finished worker's process ends, not
a death.

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

### How many tests each worker has

`tests_started` and `tests_finished` come from the worker's own state slot, and
on their own they are a numerator with no denominator: the one question anybody
watching a run actually has is *is it nearly done*, and nothing a worker writes
can answer it. **No worker knows.** It collects the whole suite and is then fed
indices a chunk at a time, so an empty queue and a pause look the same from
inside. The controller's scheduler holds what is outstanding per worker and
throws an index away the moment that test completes, so it cannot say how many
have been through either. The total exists only as the sum of the two, which is
why the controller works it out and writes it into the run's directory as
`schedule.json` — where `/workers`, the sample hook and anything else reading
the evidence pick it up like every other fact here.

| field | on | meaning |
|---|---|---|
| `tests_assigned` | worker | tests handed to this worker **so far** |
| `tests_finished` | worker | of those, how many it has run |
| `tests_running` | worker | the test in flight — 1, or 0 between tests |
| `tests_queued` | worker | the ones it has not begun |
| `collected` | run | tests in the run's whole collection |
| `unassigned` | run | tests that are nobody's yet — what every total can still grow by |
| `settled` | run | whether any worker's total can still change |

**The three worker counts partition the total**: `finished + running + queued
== assigned`, always, with every test in exactly one of them. That shape is
deliberate. Reporting what was *left* instead read more naturally and was a
trap — "not finished" includes the test in flight, and so does `tests_started`,
so the two obvious numbers to add were the two that overlapped, and a row
saying `started 2, pending 2, assigned 3` looked broken while being correct.

**It is a running total, not a plan, and `settled` is how you tell.** Under
`--dist load` and its relatives the scheduler keeps most of the suite in a queue
nobody is assigned yet and hands it out in chunks, so a worker's total grows for
as long as `unassigned` is above zero — a percentage drawn without it is a bar
whose end moves. Under `--dist each` it is settled from the first moment,
because every worker is given the whole collection at once. Under
`--dist worksteal` an empty queue is *still* not settled while a steal can
happen: that mode moves work between workers, so a total that has stopped
growing can still shrink. It takes both halves of xdist's own condition —
somebody idle to give the work to, and somebody holding more than the floor of
two to be worth taking it from — so one worker running the tail of a run is
*not* settled, while two workers holding two tests each are.

**The three numbers in a row always agree**, and that took arranging, because
they do not come from one place: the total is the controller's, written into
one file for the whole run, and `tests_finished` is the worker's own, written
into its own slot. Pairing a live read of one with a stale read of the other
produced rows saying a worker had finished nineteen of the fifteen tests it had
been given — not a lag a reader can interpret, but a row that cannot be true.

Two things stop it. The controller's record is rewritten *whenever a test
starts* rather than on a timer, so it is never more than one test behind; and
only the total comes from it — the split is measured from the worker's own
counts, and the total is floored at `tests_started`, because a worker cannot
begin a test it was never given either. A stale total can then only understate
the queue, never contradict the line above it.

What can still move is `tests_assigned`, by one, for an instant. A worker tells
the controller a test is finished before it tells the *scheduler*, so in
between it is counted as run and still outstanding both. Writing from the start
of a test rather than the end of one keeps that worker out of its own window;
what is left is the rarer case of another worker's two messages straddling this
one's, and it lasts until the next test starts somewhere.

**A rerun is the same test, not another one.** pytest-rerunfailures, flaky
and every plugin written the same way run a failed test's phases again inside
the same protocol, so setup and teardown happen once per attempt while the
test, its node id and its slot in the scheduler's queue are one. Both counters
count the test: the worker counts a start at the first setup of a protocol and
not at a second, and takes back the finish it counted at the end of the attempt
that turned out not to be the last, so a rerun in flight reads as one running;
the controller counts a teardown report that names the test the worker has just
finished as that test again. Before that, a run of 368 tests with six rerun once
was reported as 374 — every attempt a test, and the total floored at the
worker's count so that an attempt became a test nobody was given.

**A crashed worker keeps its row, so the rows can add up to more than the run.**
xdist drops a dead worker's queue back into the global one and starts a
replacement under a new id, and the test it died *in* is reported failed rather
than reassigned. The dead worker's row stays as it was — `tests_assigned: 5,
tests_finished: 3, tests_running: 1, tests_queued: 1` is "it was given five,
ran three, died inside the fourth and never started the fifth", which is the
line a death is triaged with, and the replacement gets a row of its own rather
than overwriting it. What that costs is that the tests it was given and
somebody else then ran are counted in both rows. So `collected` is the run's
size and summing `tests_assigned` is not; `status: gone` is what marks a row as
a record of a process rather than a report on one.

Nothing is written for a run with no scheduler to ask — a single-process run,
or a distributed one whose workers have not collected yet. Those fields come
back `null`, which is not zero: zero pending is a worker about to finish, and
not knowing is not.

### When there is no live view

Switching the server on and getting no server raises a
`stack_server_unavailable` incident through the same hook as everything else.
Without it the run continues perfectly well and your UI shows nothing forever
with no error anywhere — because from the outside "no server" and "no tests
running" look identical, which is the exact misreading this package exists to
prevent:

```
No live stack view this run: 127.0.0.1 could not serve on port 8080   [stack_server_unavailable PORT_TAKEN, runtime, informational]
    Port 8080 is held by something that is not a stack server (Address already in use); pass --callstack-port with an unused port, or leave it off entirely and let one be drawn.
    The run itself is unaffected; only the live view is missing.
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

Owner `runtime`, severity `informational`: no test is at fault and nothing is
broken. What is lost is a diagnostic, and somebody has to decide whether to
reconfigure it.

### Who may ask

Four things, and the token is only one of them.

**The bind.** Loopback by default, and anything else refuses to open without a
token — see below.

**The `Host` header.** A request naming a host this server never bound is
refused with 403. That is not about the network, which the bind already
settles; it is about a browser. A page you visit can re-resolve its own
hostname to `127.0.0.1`, at which point its origin *is* this server's and the
same-origin policy stops protecting you. Checking `Host` costs nothing and
closes that. A bind that is not loopback is exempt, because the address a
legitimate client outside a container uses is one this process never learns —
there the token is what stands in for the check.

**Which pids `/stack` will answer for.** This run's: the serving process, plus
the worker pids read out of the evidence directory. Anything else is 403. The
server reads any process it has permission to read, so without this a caller
who got past the bind could walk pids and collect the stack of every process
you own — and each read pauses its target.

**A token, if you supplied one.** On loopback you usually will not:

```console
$ curl localhost:8080/workers
```

With a token, which is what any bind but loopback requires:

```console
$ export PYTEST_CALLSTACK_TOKEN=$(openssl rand -hex 16)
$ pytest -n8 --callstack-host 0.0.0.0 &
$ curl -H "Authorization: Bearer $PYTEST_CALLSTACK_TOKEN" host:8080/workers
$ curl "host:8080/workers?token=$PYTEST_CALLSTACK_TOKEN"   # for a hurry
```

**The token is supplied, never minted, and never written to disk.** That is the
whole design, and it comes from the two halves of the problem being opposites.

*The port has to be published.* A port drawn at random is unguessable by
construction — that is the point of drawing it — so the run must write it down
for anything outside to find it.

*The token does not.* It is the one value both ends can agree on in advance,
because whoever starts the run picks it. Minting one here made it discoverable
instead, which meant writing it into the address file — and that turned every
question about where a run may write its evidence into a question about where a
*secret* may live. POSIX answers that with an `0o600`. Windows does not answer
it at all: a mode there is not an ACL, so the file inherits the evidence
directory's and the promise quietly stops holding on a supported platform.

So the address file is ordinary data — a host, a port and a pid, the address of
a server anyone who can reach it may query anyway. Put `failure_directory`
wherever evidence goes, on any platform. And the secret arrives the way secrets
already reach a container, a CI job and a shell:

| | |
|---|---|
| `PYTEST_CALLSTACK_TOKEN` | a shell, a CI job, `docker run -e` — prefer this |
| `--callstack-token SECRET` | one run, at the cost below |
| *(nothing)* | no authentication — the default, and right on loopback |

There is no ini setting, deliberately: ini files live in the repository.

**The two are not equally private.** `--callstack-token` puts the secret in the
controller's command line, and a command line is public on a shared machine:
`/proc/<pid>/cmdline` is world-readable on Linux, so any other account can take
the token out of `ps -eww` for as long as the run lasts — on exactly the
machine a token is worth having. Shell history and an echoed CI command keep it
after the run has ended, too. `PYTEST_CALLSTACK_TOKEN` reaches the same setting
by the same path and has none of that: `/proc/<pid>/environ` is `0400`, the
owner alone. The flag still works — runs use it, and it is unobjectionable on a
machine with one user on it — and a run that uses it warns once, saying this.

**No token is the default and the right one on loopback**, where the bind
already bounds the reachable set to processes on this machine. On a box you
share with people you would not hand a debugger to, supply one or leave the
server off — "only local" and "only you" are different statements, and without
a token only the first is being made.

**Off loopback without a token is refused**, before the socket is opened, and
reported as a `stack_server_unavailable` incident. Serving every local
process's stack to whatever can route to the host is not something anybody
configures on purpose, and a warning is the wrong instrument for it: by the
time one is read the port has been open for the length of the run.

`/identity` stays open even with a token set: it is what one session asks
another before standing down from a contested port, and two sessions that
minted nothing have no way to share a credential. It answers with a service
name, a version and a pid.

### Finding the server

The run tells you, on a hook, the moment it is serving:

```python
def pytest_failure_server_ready(server):
    registry.upsert(
        session=server.session_id,       # names this run's evidence directory
        url=server.url,                  # already bracketed if the host is IPv6
        port=server.port,                # what got bound, never the 0 you asked for
        token=server.token,              # what you supplied, or "" if you did not
        pid=server.pid,                  # the controller, not any worker
    )
```

That is the whole address, and for a drawn port it is the only way to learn it
before the run is over — nobody can configure a number that did not exist a
moment ago. `server` is a `LiveStackServer`; `server.headers()` gives you the
`Authorization` header the endpoints want — `{}` when this run supplied no
token, so the same client code works either way — and `server.endpoint("/workers")`
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
know. Where a dashboard can reach the run, that is the better route — it reports
more per worker than a sample does, at whatever cadence it chooses, and costs
nothing at all while nobody is watching.

What it needs is a listening socket, and there are runs that cannot have one: a
CI job forbidden to open a port, a container with nothing routed into it, a run
too short-lived for anything to discover and poll before it is over.
`failure_sample_seconds` turns the same information around and pushes it out of
the process instead, with no port and nothing to discover:

```python
def pytest_failure_worker_sample(sample):
    # collected / unassigned / settled ride on the sample too: a worker's
    # total is what it has been given so far, so a bar drawn without them
    # has a moving end — and this is the path for runs that cannot open a
    # port, where there is no /workers to ask instead.
    for worker in sample.workers:
        rows.insert(session=sample.session_id, at=sample.observed_at,
                    worker=worker.worker, nodeid=worker.nodeid,
                    phase=worker.phase, status=worker.status, why=worker.why,
                    rss_mb=worker.rss_mb, cpu_rate=worker.cpu_rate,
                    assigned=worker.tests_assigned, done=worker.tests_finished,
                    queued=worker.tests_queued)
```

A run with no workers pushes one row per pass, for `main`, from the same files.

Off by default. It is the only hook here that fires when nothing is wrong, so it
is the only one with a running cost — and that cost is a directory walk: every
field above comes from the `.state` and `.events` files the run was writing
anyway, and nothing is asked of a worker itself. No ptrace, no subprocess, no
pause. A sample of sixty-four workers is a few kilobytes of statuses.

**No frames, deliberately.** Reading a stack per stuck worker per pass was tried
here and taken out again: `blocked` is the status of any worker under 0.05
cores, so on an I/O-bound suite every healthy worker waiting on a database
qualified, and each pass paid a subprocess and a pause for each of them. Frames
are worth that when a human is asking about one worker — `/stack?pid=`, on
demand — rather than for every stuck worker on a timer. `session_id` and the
worker's pid are what join a sample to a stack fetched that way.

### Containers

`--callstack-host 0.0.0.0` is what a container needs: its UI is outside, and
127.0.0.1 inside a container is unreachable from there. That bind requires a
token and is refused without one — see [Who may ask](#who-may-ask) — which
suits a container better than the alternative did:

```console
$ docker run -e PYTEST_CALLSTACK_TOKEN -p 8080:8080 yourimage \
      pytest -n8 --callstack-host 0.0.0.0 --callstack-port 8080
```

The UI outside reads the same value from the same place. Nothing has to be
mounted out for it to find a secret in a file, which is what a minted token
would have required.

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

A worker therefore nominates its parent as a permitted tracer at startup, via
`prctl(PR_SET_PTRACER, <controller pid>)` — the exception Yama provides for
exactly this. `worker_start` records whether it was granted, so a refused read
has an answer beside it rather than only a message.

Yama admits the nominated pid **and every descendant of it**, so this is wider
than "the controller's py-spy may read this worker". The controller's
descendants are the whole process tree of the run: every other worker, and any
subprocess a test spawns while the declaration stands. The reader it exists for
is one of them and is not the only one.

**So the declaration is only made where something is going to read a worker's
stack** — the live stack server, or the sampler (`failure_sample_seconds`) —
and is `off` on every run where neither is on, which is most runs. The
controller resolves that, being the only process that can see either, and hands
each worker the answer; a worker never judges it for itself. `failure_tracer`
says *which* declaration such a run makes, not that one is made.

A *named* port shared across sessions still reads only the workers of the
session hosting it: another session's workers nominated *their* controller, not
this one. `failure_tracer = any` is what lifts that — it drops the relationship
requirement entirely, so any reader on the machine that could already ptrace is
permitted. That is the setting a shared server needs and the one a private run
does not, which is why it is not the default.

### Reading a live process is py-spy's job

py-spy is installed with the package. There is no way to walk another
process's frames from Python, so a live stack is read from outside the
target: py-spy reads its memory rather than asking it to run anything, and
stops it before reading, so it never walks a frame that is being torn down.
That is what makes it work on a worker whose GIL is held by native code, and it
is why nothing here has to be signalled.

**Every pid is read that way, including the process doing the reading.** Its
own frames are also directly to hand — `sys._current_frames()`, no subprocess
and no permission — and answering from them was a second reader with a second
`source` for the one process that could avoid the first. A caller that has to
know which mechanism answered has been handed two APIs, and a UI is where that
ends up encoded. One reader, one shape, one thing to keep working.

Reading yourself is a child tracing its parent, which is the wrong direction
for Yama. The declaration that admits it names *this* process — so it permits
this run's own descendants and nothing else, narrower than the `parent` policy
above — and it is made at the moment of the read rather than at startup, so
only the runs that read a stack make it.

Where py-spy cannot read the target — macOS without root, a sibling under Yama
— the endpoint still answers, with the reason instead of a stack.
A UI that is told *why* it has no stack can tell a dead process from a missing
permission; one that gets an empty response cannot. The same is true of a
stall: the verdict comes from heartbeats rather than frames, so it is reached
either way, and the stack falls back to whatever the watchdog last wrote with
its age attached.

On Windows there is no ptrace and no equivalent restriction: any process can
read another running as the same user at the same integrity level, so the
descendant rule above simply does not apply. Reading an *elevated* process from
an unelevated one needs `SeDebugPrivilege`.

## Profiling

Everything above waits for something to go wrong. `--failure-profile` waits for
nothing: it samples the process running the tests for the whole run and, at
the end, names the functions that burnt the CPU and the tests that kept the
memory — as incidents, through the same hook, because "your image comparison
is 38% of the run" is a finding you want flagged the way a segfault is.

```console
pytest --failure-profile
```

or `failure_profile = true` in ini for every run. Off by default: it is the
one thing here with a running cost on every healthy test, rather than only on
a run that went wrong. What that cost is, is not a claim — `benchmarks/profile_gate.py`
measures it on the same fixed-CPU workload with and without the sampler and
fails the build past 1.10x serially and 1.12x on four xdist workers, and CI
runs it on every push. See "Cost".

### What it measures

A thread of its own wakes fifty times a second on each worker, reads every
thread's stack, and charges the CPU each thread burnt since the last wake to
the stack it is in now. **Weighted by CPU, not by wall time.** A thread asleep
in `recv` weighs nothing and vanishes; a thread spinning on a poll becomes the
widest thing in the profile. That is the difference between "where did the
time go" and "where did the cores go", and only the second answers why a
worker sits at 30%.

The per-thread counters come from `clock_gettime` on each thread's CPU clock,
which reads in a few hundred nanoseconds and — this is the part that matters —
without releasing the GIL. A sampler that opens a file per thread per sample
releases the GIL on every read and then waits a whole switch interval to get it
back from a busy test; measured, that ran a fifty-hertz sampler at eight.

A function is named the way it is written — `Poller.run`, not `run`, and a
comprehension under the function it is written in rather than as a `<listcomp>`
nobody wrote. 3.11 and later carry that name on the code object; on 3.9 and
3.10, which this package supports, it is worked out once per function from the
module that defines it, at the end of a test rather than on the sampling path.

Each stack is charged to the first frame, walking outward from the innermost,
that belongs to somebody: your packages or the customer's tests first, a
dependency failing that, the runtime failing everything. So two million calls
into a C pixel accessor are charged to *your* `is_images_different`, and a
second spent in `json/encoder.py` is charged to *your* `render_report`, below
the json encoder. Threads Python has no stack for — a thread pool a C extension
started — are counted by their kernel names, so CPU outside the interpreter is
reported rather than quietly dropped.

Per test it also records resident memory at every phase boundary, the peak
between them, Python's live-object count, and — on glibc — how much of the
heap is actually in use, so memory a test *freed* that the allocator kept
mapped is told from memory that is still alive.

And it keeps a **timeline**: every tenth of a second, the CPU the process
burnt in that window and how busy the whole machine was. A share of the run's
CPU is the wrong question for a suite that waits on I/O ninety-nine seconds in
a hundred — the hundredth that pins a core is invisible in it — and the
timeline is where that hundredth shows as a step with a start, a length and a
height.

A tenth of a second of *wall time*, and a window never spans less than one
sample — so where the sampler cannot tick that often, because the machine is
loaded or the platform's per-thread read is expensive, the timeline is as fine
as the sample rate allows and no coarser. It used to be five samples, which is
a tenth of a second only while the sampler is getting the CPU it asked for: at
ticks 150 ms apart a burst of 0.6 s became one window, one window is below the
two a burst needs, and the finding was not raised at all rather than raised
less precisely.

### What it raises

Three kinds, all `severity=informational` whoever owns them, because nothing
failed.

**`cpu_hotspot`** — one per function over `failure_profile_cpu_share` percent
of the run's CPU (and at least half a second of it). The verdict says what kind
of cost it is:

| Verdict | Means |
|---|---|
| `PYTHON_CODE` | the function's own lines are hot: a Python loop, or C calls made from them that leave no frame |
| `LIBRARY_CALL` | the cost is under a library or runtime call it makes, which is named |
| `BACKGROUND_THREAD` | the CPU is on a thread that is not running the test — a poller, a watcher — and is paid whatever test is in flight |
| `GC_PRESSURE` | the collector took more than a tenth of the run, and here are the tests that drove it |
| `NATIVE_THREADS` | CPU in threads Python has no stack for, by kernel thread name |

**`cpu_burst`** — from the timeline. A window is busy when the process burnt
`failure_profile_burst_cores` cores' worth of CPU over it, and a burst is a
run of busy windows. Each finding carries the stack that was there for most of
the burst, blamed like a hotspot:

| Verdict | Means |
|---|---|
| `LONG_BURST` | one test held a core for `failure_profile_burst_seconds` or longer, with when it started, which phase, and how much of the test's CPU is in that one stretch |
| `RECURRING_BURST` | the same function burst in five or more tests, whatever the length of each — a fixture, or a helper doing per call what it could do once. One finding, with the total |
| `BACKGROUND_BURST` | a thread that is not running the test held a core — between tests or under one. What a worker at a steady percentage with nothing to blame is doing |
| `CONTENDED` | the machine was over 90% busy for most of the run and the workers got slices of a core while it was. Twenty workers on four cores; nothing on any stack explains it and the machine's own figure does |

**`memory_profile`** — per test, against `failure_profile_retained_mb`:

| Verdict | Means |
|---|---|
| `RETAINED_AFTER_TEST` | the worker was left holding more than it started with, still in use, with the phase it arrived in — `setup` is a fixture |
| `HEAP_NOT_RETURNED` | it was left holding more, but none of it is in use: the allocator kept freed pages mapped. Fragmentation, not a leak |
| `TRANSIENT_PEAK` | the test climbed and came back down: what decides how many workers fit on the machine |
| `STEADY_GROWTH` | the worker drifted upward over its tests, none of them enough to be raised alone and no single step half of it, with the live-object count rising — two megabytes a test, which is the shape of a leak and the one no per-test check sees |
| `WORKER_IMBALANCE` | one worker peaked at twice its siblings, with the test after which it stood clear |
| `PEAK_OVER_CEILING` | a test climbed to `failure_profile_peak_mb` or past it, whatever it started from — the size is the finding, and it is raised even when the memory came back |
| `ALLOCATOR_RETENTION` | the worker grew by the threshold over its run and nothing is using the growth: memory the allocator was handed back and kept mapped. One finding for the run, saying which of the two causes it is — thread arenas each keeping what they freed, which `MALLOC_ARENA_MAX=2` fixes, or one main heap fragmented by small survivors, which `malloc_trim` fixes and the arena variable does not |

A memory finding about one test also carries **the code that was running
while the memory climbed**, blamed and attributed the way a crash is. The
sampler charges every rise it sees in resident memory to the test thread's
stack at that moment — or to the stack it was in a tick earlier, when the
code running at the reading is the runtime's own, which is what a test body
that allocated and returned within one reading looks like from outside. A
climb with nobody's code on either stack is reported as unplaced rather than
blamed on pytest. So a test that reads a file whole instead of streaming it
comes back as:

```
Memory over the ceiling: tests/test_loading.py::test_loads_the_export reached 1532 MB, ceiling is 1200 MB   [memory_profile PEAK_OVER_CEILING, product, informational]
    Memory rose by 1611 MB, summed over every reading that found it higher; all of that increase happened while load_everything (loader.py:15) was running, called from test_loads_the_export (test_loading.py:11).
    The memory was released before the test ended.
    Look at: loader.py:15
    Measured: process 377 MB before, 1532 MB peak, 395 MB after. Ceiling from failure_profile_peak_mb.
```

What a test *kept* is measured as the larger of the resident step and the
live-heap step. Resident memory understates it whenever the test fills pages
an earlier test freed and the allocator held on to, and the heap figure is not
fooled by that.

The live heap is glibc's `mallinfo2` and nothing else (see the platform table).
Where there is none — macOS, Windows, musl — resident memory that stayed up is
all there is to go on, so a test that freed what it allocated but left the
pages mapped reads as `RETAINED_AFTER_TEST` rather than `HEAP_NOT_RETURNED` or
`TRANSIENT_PEAK`. The size and the phase are right either way; what cannot be
had there is the sentence that separates a leak from fragmentation, and
`ALLOCATOR_RETENTION` is not raised at all. Rerunning with
`--failure-profile-allocations` is the answer on those platforms: tracemalloc
measures Python's own live allocations everywhere.

The worker that "freed everything and still sits at four gigabytes" is the
one case none of the per-test rules can name, because no test did it: a few
megabytes of freed-but-mapped memory per test, over a long worker, is under
every threshold and in use by nothing. `ALLOCATOR_RETENTION` is the rule over
the whole worker. Every record carries glibc's own account of its arenas —
how many, how much free memory sits in each, how much of that a
`malloc_trim` would return — read through `malloc_info` at test boundaries,
and when resident memory has grown by the threshold more than the live heap
did and the allocator's free figure accounts for the gap, the finding says
where the free memory is:

```
Memory held by the allocator: worker main has 289 MB that tests freed and the C allocator has not returned to the OS   [memory_profile ALLOCATOR_RETENTION, runtime, informational]
    No Python object holds this memory. It is inside glibc's heaps, mapped and unused.
    144 MB of the 289 MB is in the main heap, 145 MB in thread arenas. 11 arenas existed for up to 10 threads on 4 cores.
    Biggest single steps: tests/test_arenas.py::test_ingest_batch[0] (140 MB on main).
    glibc keeps freed memory mapped inside each arena, and gives every thread that allocates an arena of its own, up to eight per core. MALLOC_ARENA_MAX limits how many thread arenas exist.
    Measured: process 42 MB at the start, 881 MB at the end, up 839 MB over 73 tests with 466 MB of that in use.
```

A test that left a threshold's worth of it in one step would have been
`HEAP_NOT_RETURNED` on its own; under the worker's finding it is one of the
steps rather than a row of its own, since it is the same memory and the same
fix.

Free memory mostly in the main arena is the other cause — one heap
fragmented by small survivors between the big allocations — and the finding
says so instead, with what `malloc_trim(0)` would hand back right now, because
`MALLOC_ARENA_MAX` does nothing for that one. glibc only, like the live-heap
reading; elsewhere the finding is never raised.

### Allocation tracing

The sampler sees the stack that was *running* while memory climbed, which is
usually the answer and sometimes is not: a loader that hands its result to a
cache is running when the memory arrives and is not what holds it. For that
rerun the tests the plain profile named with

```console
pytest --failure-profile-allocations tests/test_loading.py
```

which switches tracemalloc on for the run — every platform, no attaching, no
debugger — and adds to each finding the lines holding the memory at the peak
and the lines holding what the test kept, and to a `STEADY_GROWTH` finding
the lines holding what the worker accumulated over the whole session. The
memory figures then come from tracemalloc itself (Python allocations only,
the tracer's own tables left out), because the tracer churns the allocator
enough to leave resident memory up after a test freed everything. Every test
that climbed also gets a **memory flame graph**: its live allocations at the
peak, by traceback, weighted in bytes, beside its CPU one.

The growth finding above, rerun that way over the two modules it names:

```
Memory growing across tests: worker main kept 289 MB in use over 48 tests, about 6 MB per test   [memory_profile STEADY_GROWTH, customer-code, informational]
    No single test kept enough to be reported on its own. 48 of the 48 tests each ended with more in use than they started with.
    Most of it during: tests/test_memory.py::test_leaks_a_little (167 MB over 7 tests), tests/test_drift.py::test_response_is_cached (122 MB over 40 tests).
    Held at the end of the worker: 190.7 MB allocated at test_memory.py:52, called from python.py:167, _callers.py:121, _manager.py:120.
    Held at the end of the worker: 145.9 MB allocated at test_memory.py:28, called from python.py:167, _callers.py:121, _manager.py:120.
    Held at the end of the worker: 122.1 MB allocated at test_drift.py:13, called from python.py:167, _callers.py:121, _manager.py:120.
    Measured: traced memory 290 MB before the first of these tests, 460 MB after the last. Biggest single step 24 MB. +20,264 Python objects per test.
```

Three lines, each an `append` to something module-level. A tracemalloc
frame knows the file and line and not the function, which is why those lines
carry no function name.

**Nothing else may be tracing.** `tracemalloc` has one set of tables per
process, and a second owner of them gets the first one's frames at the first
one's depth: both profilers then report numbers that are neither's. So a run
asked for `--failure-profile-allocations` while a tracer is already going —
another allocation profiler, `PYTHONTRACEMALLOC`, `-X tracemalloc`, a
`tracemalloc.start()` in a `conftest.py` — stops with a usage error naming the
depth it found, rather than starting. It is the one thing in this package that
ends a run instead of warning and carrying on, and it is deliberate: everything
else here reports a failure that has already happened, where being quietly
degraded is better than being absent, and this one *is* the measurement, where
being quietly wrong is worse than not running. The plain `--failure-profile`
never touches the tracer and never raises this; neither does
`failure_tracemalloc_depth`, which attaches to whatever is already tracing.

Tracing costs three to six times on allocation-heavy code and far more on a
tight loop of small objects, which is why it is a rerun and not the nightly —
and why a traced run raises **no CPU findings**: its CPU figures are the
tracer's as much as the tests', and the summary says so. Memory is what it is
for. `failure_profile_allocation_depth` is how many frames each allocation
keeps.

This is what the example suite under
[`examples/profiling`](examples/profiling) prints, trimmed:

```
CPU hotspot: load_everything (loader.py:15) used 11% of this run's CPU, 1.9 s   [cpu_hotspot PYTHON_CODE, product, informational]
    The time is in this function's own lines, not in calls it makes. Mostly line 15 (68%), line 16 (32%).
    Seen in 1 test: tests/test_loading.py::test_loads_the_export.
    Look at: loader.py:15

CPU on a background thread: 'status-poller' used 11% of this run's CPU, 1.9 s, in Poller._run (poller.py:30)   [cpu_hotspot BACKGROUND_THREAD, product, informational]
    This thread is not the one running tests, so it uses this CPU whichever test is executing.
    Seen in 13 tests: tests/test_polling.py::test_with_the_poller_running, tests/test_polling.py::test_another_with_the_poller_running, tests/test_sessions.py::test_request_answers[1] and 10 more, and between tests.
    Look at: poller.py:30

Memory kept after test: tests/test_memory.py::test_big_fixture ended with 143 MB more in use than it started with   [memory_profile RETAINED_AFTER_TEST, customer-code, informational]
    Memory rose by 166 MB, summed over every reading that found it higher; all of that increase happened while big_fixture (test_memory.py:34) was running, called from call_fixture_func (fixtures.py:1005).
    The increase happened during setup, so a fixture allocated it, and it was still in use after teardown.
    Look at: test_memory.py:34 and what holds its result after the test.
    Measured: process 512 MB before, 655 MB after. Live heap +143 MB. +429 Python objects.

Memory growing across tests: worker main kept 295 MB in use over 71 tests, about 4.2 MB per test   [memory_profile STEADY_GROWTH, unknown, informational]
    No stack was captured; the owner is taken from the test's file, tests/test_allocation.py (customer-code).
    No single test kept enough to be reported on its own. 52 of the 71 tests each ended with more in use than they started with.
    Most of it during: tests/test_memory.py::test_leaks_a_little (167 MB over 7 tests), tests/test_drift.py::test_response_is_cached (122 MB over 40 tests), tests/test_arenas.py::test_ingest_batch (6 MB over 4 tests).
    Look at: rerun those tests with --failure-profile-allocations to see which lines hold the memory.
    Measured: process 43 MB before the first of these tests, 882 MB after the last. Biggest single step 24 MB. 295 MB of the 576 MB increase is in use; the rest was freed and kept by the allocator. +501 Python objects per test.
```

And the I/O-bound suite under [`examples/profiling/tests/test_polling.py`](examples/profiling/tests/test_polling.py)
and its neighbours, where the timeline is what finds the fixture — no single
one of its six bursts is worth a look, and a share of the CPU says nothing
about *when* it was spent:

```
Repeated CPU burst: Session.__init__ (session.py:30) ran at full CPU for about 0.4 s in each of 6 tests, during setup   [cpu_burst RECURRING_BURST, product, informational]
    2.5 s of CPU in total across the 6 bursts. Called from session (test_sessions.py:13).
    Tests: tests/test_sessions.py::test_request_answers[5], tests/test_sessions.py::test_request_answers[3], tests/test_sessions.py::test_request_answers[4] and 3 more.
    Machine load during these bursts: 26%.
    Look at: session.py:30. It ran during setup of each of those tests.

CPU burst: tests/test_index.py::test_index_is_complete ran at 1.0 cores for 2.7 s, starting 1.1 s into the test, during call   [cpu_burst LONG_BURST, product, informational]
    Running build_index (reports.py:42), called from test_index_is_complete (test_index.py:13).
    This burst is 87% of the test's 3.4 s of CPU. The other 1.5 s of the test's 4.8 s was waiting.
    Machine load during the burst: 26%.
    Look at: reports.py:42
```

The same run prints a summary at the end of the terminal output — the run's
CPU against its wall time, what each worker peaked at, and the top functions:

```
Profile: 74 tests, 27 s of wall time, 18 s CPU (0.69 cores on average), 2.5 s of it in garbage collection
  worker main: 74 tests, 18 s CPU, peak 1532 MB, 882 MB at the end
Functions using the most CPU:
   16.8%    2.98 s  build_graph  test_allocation.py  [customer-code]  in 2 tests
   14.3%    2.53 s  render_report  reports.py  [product]  in 2 tests
   13.6%    2.42 s  build_index  reports.py  [product]  in 1 test
   13.4%    2.37 s  is_images_different  image_compare.py  [product]  in 3 tests
   13.1%    2.33 s  Session.__init__  session.py  [product]  in 6 tests
```

Every finding is printed the way every incident is (see "How an incident
reads"): a first line that says what was measured, in words, ending with a
`[kind VERDICT, owner, severity]` tag; then lines that are each a
measurement, what it means by how it was taken, or a place to look.

It also writes a [speedscope](https://www.speedscope.app/) flame graph for every
test a finding names, and for the gaps between tests, under
`<run directory>/profiles/` (`<test>-<hash>.speedscope.json` for CPU, and
with allocation tracing on `<test>-<hash>.memory.speedscope.json` for the
allocations at its peak; the hash is of the full node id, so two names that
sanitise alike cannot overwrite each other). The raw per-test records are in `<worker>.profile.jsonl` beside
the rest of the evidence, timeline included.

### What it cannot see

Reliable native-call attribution is outside the supported profiling contract.
Sampling from inside the process needs the GIL. A native call that holds it
can finish before the sampler observes its Python wrapper. CPU counters still
measure the work on a surviving thread, but its cost may be assigned to a
previous or subsequent sampled function. A Python function that mixes Python
and native work has the same limitation during its native portions. GIL-releasing
native calls are easier to sample, but exact native-call attribution is not
guaranteed either. Use the profile to investigate Python hotspots and bursts;
do not treat a missing native caller as evidence that it used no CPU.

Per-thread clocks use POSIX thread clocks on Linux, `thread_info` on macOS,
and `GetThreadTimes` on 64-bit Windows with psutil fallback. Windows counters
are coarse against the sampling interval. Where per-thread clocks are
unavailable, process CPU is charged to the test thread and the report says so.
Threads that start and exit between samples can go unseen.

The native qualification probe remains a visible, non-blocking diagnostic:
its JSON preserves failed attribution measurements. Python detection, quiet
workloads, overhead, resource budgets, and the full test suites remain release
gates. This is an accepted limitation, not a claim that native attribution
was repaired. The live-heap reading that tells freed-but-mapped memory
from kept memory is glibc only; elsewhere what a test kept is its resident
step alone.

## One directory per run

```
.pytest-failures/
  .gitignore           <- so the directory never reaches a commit
  run-70a514cc7a93/    <- this pytest process's own name for itself
    owner.json         <- the controller's pid, and the only thing that makes
    gw0.state             this directory ours to delete
    gw0.events         <- every line carries the *reported* run id
    gw1.state
    schedule.json      <- how many tests each worker has been given, which is
    callstack-4213.json   the one fact no worker can write about itself
```

A run with no workers writes the same files under `main`, since it is the
process running the tests:

```
.pytest-failures/
  run-8f21c0b4e5d7/
    owner.json
    main.state
    main.events
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
make two runs deliberately share one. The value has to be a *name* rather
than a path: 1–128 characters of letters, digits, `.`, `-` and `_`, and
neither `.` nor `..`. Anything else is refused with a warning and the run
names itself — so a slugified branch works and a raw `feature/x` does not,
because a separator in there would put this run's evidence somewhere other
than `failure_directory`.

**What gets cleaned up, and what is read first.** Whole directories of runs
that are *over* — not old — and never before they have been asked whether they
have anything left to report.
The controller's pid is in `owner.json`, so a run still going is recognisable
as such however long it has been going, which matters precisely because several
run at once. A directory without that marker is not ours and is left alone
whatever it looks like, which includes the flat files an older version of this
plugin left behind: they cannot be mistaken for a current run's evidence,
because a current run does not look there for any.

### The directory keeps itself out of git

The default directory is inside somebody's checkout, and what lands in it is
one run's scratch that a later run deletes — so the first run to make it writes
a `.gitignore` of `*` at the top, and nothing here ever turns up in a `git
status` or in an unlucky `git add -A`. Nothing has to be added to the
repository's own `.gitignore`, which means the second checkout, the CI image
and the colleague who just installed the plugin all behave the same.

It is written only into a directory holding nothing but run directories of
ours, and an existing `.gitignore` is never rewritten. `failure_directory` is a
natural thing to point at an artifacts directory somebody else also writes to,
and `*` dropped in there would quietly stop git from seeing *their* files —
a change to their repository that this plugin has no business making. The same
rule is what makes `failure_directory = .` harmless: the checkout is full of
files that are not ours, so it gets no ignore file rather than one that hides
the whole repository.

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
- Per test, on the controller only: `schedule.json` rewritten, which is a
  length read off the scheduler per worker and one small write at a fixed
  offset — 6µs at eight workers, 32µs at sixty-four. It was written by rename
  at first, at 54µs a time, and throttled to twice a second to pay for that;
  the throttle is what let a worker's row contradict itself, so the write got
  cheap instead. Nothing here scales with the number of *tests*: diffing a
  queue against its last reading would, and so would reading the loadscope
  family's per-test done flags — that one was measured at 1.7ms a write on a
  sixty-thousand-test `--dist loadfile` run, near two minutes of controller
  time over the run, and is now a length per scope instead (133µs, 8s).
- Off by default, and the only thing here that costs a *passing* test
  anything: the profiler. One thread per worker, waking fifty times a second,
  reading each thread's CPU clock with a syscall that keeps the GIL and
  walking its stack — and every tenth of a second, the machine's load;
  resident memory and the live heap every other tick. Measured against plain
  pytest on the same fixed-CPU workload by `benchmarks/profile_gate.py`,
  which CI runs on every push and which fails past 1.05x for instrumentation
  alone, 1.10x for serial profiling and 1.12x for profiling under four xdist
  workers. Allocation tracing is outside those budgets on purpose: it is a
  rerun over the tests a plain profile named, and its cost is the workload's
  (see "Allocation tracing").
- Per profiled test, on disk: one JSON line of about 3 KB appended to
  `<worker>.profile.jsonl` as the test ends. It is the one thing this package
  writes that grows with the number of tests, and the controller reads all of
  it back at session finish — so a 20,000-test run folds 62 MB of log, which
  is where the profiler's memory goes and when: at the end of a long run,
  which is when a container is least likely to have it spare. The log is read
  a line at a time rather than whole, and the strings every record repeats —
  its frame table, which is the same paths on every test, and its keys — are
  shared across the run rather than copied per record. Measured on 20,000
  records: 346 MB to 145 MB, and slightly faster.
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

All of these are read only once a run has switched the plugin on —
`--failure-instrumentation` on the command line or in `addopts`, a
`--callstack-*` option, or a call to `install`. Registered without it, they are
accepted and inert.

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
| `failure_kill_trace` | `true` | Observe kills using Linux tracefs or Windows ETW where permitted, while preserving signal masks and handlers. See [Who killed it](#who-killed-it) |
| `failure_elevate` | `false` | Allow `sudo -n` for the witnesses that need root: `dmesg` where `/dev/kmsg` and the journal are closed, and the tracepoint above |
| `failure_capture_output` | `false` | Keep the last few KB of each worker's captured stderr, so a native crash message survives the kill that swallows it. See [What the worker last said](#what-the-worker-last-said) |
| `failure_on_run_death` | — | A dotted path `package.module:attribute` to a callable the sidecar calls with each incident of a run whose controller was killed, so a killed run is reported at once. From Python, `install(config, on_run_death=functools.partial(...))`. See [Reporting a killed run from the sidecar](#reporting-a-killed-run-from-the-sidecar) |
| `failure_tracer` | `parent` | Who may read a worker on Linux under Yama: `parent`, `any`, `off`. Declared only when the stack server is on — a run with no reader declares nothing whatever this says |
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
| `failure_profile_allocations` | `false` | Trace allocations with tracemalloc as well, naming the lines that hold the memory and writing memory flame graphs; tens of times slower on allocation-heavy pure-Python code, so for a rerun of the tests an untraced run named (`--failure-profile-allocations` for one run, which implies `--failure-profile`) |
| `failure_profile_allocation_depth` | `12` | Frames kept per allocation when tracing |
| `failure_profile_burst_cores` | `0.7` | Cores' worth of CPU a tenth-of-a-second window must hold to be part of a burst |
| `failure_profile_burst_seconds` | `2` | Seconds a burst must last to be raised as one test's own; the same function bursting in five tests is raised whatever their length |

There is deliberately **no ini setting for the token**. It comes from
`PYTEST_CALLSTACK_TOKEN` or `--callstack-token` and nowhere else: an ini file
lives in the repository, and a credential in the repository is the thing this
design exists to avoid. Prefer the environment variable — a token on the
command line is readable by every other user of the machine. See
[Who may ask](#who-may-ask).

`failure_slow_test_seconds` and `failure_stall_seconds` are not independent.
The stack a stalled worker is reported with is whatever the watchdog last
wrote, so the cadence has to have fired before the stall is assessed — a stall
judged sooner is judged with no stack at all, and on Windows that is every
stall. Neither is clamped, but an inverted pair warns.

`failure_crash_stack` is a trade rather than a switch, and it is off because of
which way the trade goes by default. `faulthandler` keeps exactly one
destination for a fatal signal, and pytest's own plugin has already pointed it
at stderr. A worker takes it and loses nothing — that stderr is shared with
fifteen other workers and a dump written into it belongs to nobody. A run with
no workers would be taking the crash out of a terminal somebody is watching, in
exchange for one that cannot be reported until a later run reads the file.
There is no having both: `faulthandler.register(SIGSEGV, chain=True)` is
refused by CPython itself.

What is lost by leaving it off is the *stack*, not the report — see the
recovered incident above, which still names the test, the phase, the counters
and the memory, and still offers a `suspect_owner`. What it cannot carry is a
blamed frame. Turn it on where the incident is the artefact that gets read and
the terminal is not, which is most of CI.

`failure_memory_limit_mb` is worth a note: an `RLIMIT_AS` cap makes the
allocation fail *inside* the process, so you get a `MemoryError` with a
traceback and a node id instead of an uncatchable kill with neither. It costs
you a hard ceiling per worker, which is why it is opt-in.

`failure_directory` is safe to share between runs going at once, and it has to
be: worker ids start at `gw0` in every run, so a flat directory would have two
sessions writing the same file names. Each run gets a subdirectory of its own
named for its session, holding an owner marker with the pid that made it.
Cleanup prunes *whole directories whose owner is no longer running* rather than
a list of file suffixes, so a live run's evidence is never what a starting run
deletes, and a coverage report that happens to live there is never touched at
all.
A directory the plugin has to itself also carries a `.gitignore`, so the
evidence never reaches a commit; one it shares with somebody else does not, for
the same reason the cleanup leaves their files alone.

Two things back that up rather than repeating it. Every record carries the run
that wrote it, and a reader refuses one naming a different run — so even a
directory that somehow got crossed yields missing evidence rather than another
run's attributed to yours. And a run that finds a live session already owning
its directory says so.

## Platform coverage

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| Test in flight, phase, exit status | yes | yes | yes |
| Crash stack | yes | yes | yes |
| Stack from a *slow or hung* test | yes | yes | yes |
| Current memory | procfs | psutil | psapi |
| Container limit, OOM counter | yes | n/a | n/a — no OOM killer |
| On-demand stack from a stalled worker | yes | yes | no |
| Live stack of a running process (py-spy) | yes | root only | yes |
| Stack from a worker that stopped running Python | yes | yes | yes |
| Who sent the SIGKILL (signal tracepoint, root or sudo) | yes | no | — |
| Who called `TerminateProcess` (ETW audit event, administrator) | — | — | yes |
| The OOM killer's own record of the victim and the fleet (kernel log) | yes | n/a | n/a — no OOM killer |
| SIGTERM sender without kernel tracing | unknown | unknown | n/a |
| Profiler: CPU per thread | thread clocks | mach `thread_info` | psutil (16 ms ticks) |
| Profiler: live heap, to tell freed-but-mapped from kept | glibc `mallinfo2` | no | no |
| Profiler: arenas, to tell `MALLOC_ARENA_MAX` from fragmentation | glibc `malloc_info` | no | no |
| Profiler: bursts, drift, allocation tracing, memory flame graphs | yes | yes | yes |

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

- **the profiler's budget**, `benchmarks/profile_gate.py`, which is a job of
  its own because it is the one claim in this README a reader cannot check by
  reading. It times the same fixed-CPU workload under plain pytest, under
  instrumentation and under the profiler, serially and on four xdist workers;
  it asks five times whether a known sustained hotspot is found, and five
  times whether a quiet run stays free of CPU findings. Every raw timing is in
  its JSON output. A budget moves only with the measurements that say why.

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

All nine kinds and every verdict in the tables above are covered, on all three
platforms, with and without xdist — a run with no workers records, serves,
watches and is recovered through the same code paths a distributed one uses,
and the tests drive it the same way: a real run, wedged or killed for real,
read back through the hook.

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
