# pytest-failure-instrumentation

When a pytest-xdist worker dies, this is all you get:

```
[gw7] node down: Not properly terminated
```

That string is a placeholder xdist substitutes when a worker's channel closed
without the remote sending anything. An OOM kill, a segfault in a C extension
and a stray `os._exit()` are indistinguishable at that point, because the cause
never left the dead process. A worker that *stalls* is worse still: no hook
fires at all, and the run simply never ends.

This records what a dying process cannot say afterwards, works out whose code
is responsible, and hands you one incident per problem.

```
[worker_death] NATIVE_CRASH  severity=critical  owner=product
    blamed on engine.py:5 in native_call
    in flight test_crash.py::test_uses_native_path  phase=call
    · died while running test_crash.py::test_uses_native_path (call)
    · exit status -11 - SIGSEGV: segmentation fault in native code (pid 19201, via waitid)
```

## Install

```console
pip install pytest-failure-instrumentation
```

It registers itself. Implement one hook to receive what it finds:

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
| `internal_error` | `InternalErrorIncident` | any run |
| `run_summary` | `RunSummaryIncident` | any run |

Only worker deaths are a distributed problem. An internal error ends a
single-process run just as finally, through a path that produces no terminal
summary at all, and the run summary is what says a run reached its end — so
the plugin registers whether or not you run under xdist, and a plain `pytest`
gets both.

They share `verdict`, `confidence`, `severity`, `owner`, `fingerprint`,
`run_id`, `worker` and `evidence`. `str(incident)` is the alert text — the
blocks quoted in this README are what it prints. A stored row comes back as the
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
framework defect that ends the run: nobody's test is at fault, so nothing else
will ever surface it.

**`fingerprint`** is stable across runs and excludes worker id, pid and
timings, so one defect on twelve workers is one incident with a count.

**`capabilities`** says what the machine could measure. A missing memory figure
on a customer's Windows box means "unmeasurable here", not "fine".

## Verdicts

| Verdict | Told apart by |
|---|---|
| `OOM_KILLED` | `-9` **and** the cgroup OOM counter moved |
| `SIGKILLED` | `-9`, counter flat — host OOM, CI cancellation, external kill |
| `NATIVE_CRASH` | SIGSEGV/SIGABRT/SIGBUS/SIGILL/SIGFPE, or a Windows NTSTATUS |
| `SIGNAL_<n>` | SIGTERM/SIGINT/SIGHUP — a request to stop, not a defect |
| `SELF_EXIT` | clean exit code, no signal |
| `PROBABLY_SIGNALLED` | exit code 128–191, a wrapper ate the signal |
| `UNKNOWN` | no status obtainable (remote gateway) |

An exit status of `-9` is identical for the OOM killer, a cancelled CI job and
a stray `kill`, so only the cgroup counter licenses the OOM verdict. Without
it, the honest answer is that it was killed.

## Cost

The rule is that a passing test must cost as close to nothing as possible,
because that is the overwhelming majority of what runs.

- Per test: two fixed-size writes to a file that never grows, plus arming a
  watchdog timer (~78 µs). No append log, no `/proc` read, no allocation
  tracking.
- Per 5 seconds, per worker: one heartbeat carrying CPU time and resident
  memory — bounded by wall-clock time, not by how many tests run.
- Off by default: `tracemalloc` (needed to attribute an OOM kill to a source
  line) and the live-object census (walking the heap on a worker near its
  ceiling is exactly the instrumentation that makes things worse).
- pydantic is imported on the controller, and only when xdist is active. A
  worker never loads it, so nothing about the per-test path changed when the
  payload became typed.

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

## Platform coverage

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| Test in flight, phase, exit status | yes | yes | yes |
| Crash stack | yes | yes | yes |
| Stack from a *slow or hung* test | yes | yes | yes |
| Current memory | procfs | psutil, else peak only | psapi |
| Container limit, OOM counter | yes | n/a | n/a — no OOM killer |
| On-demand stack from a stalled worker | yes | yes | no |

Windows has no `SIGUSR1` and `os.kill` there cannot deliver one, so a stalled
worker cannot be asked for a stack on demand. It is asked in advance instead:
every test arms `faulthandler.dump_traceback_later`, so anything that outlives
`failure_slow_test_seconds` writes its own stack. That works everywhere, and it
interrupts no syscall — a signal can nudge a C extension blocked in a syscall
that ignores `EINTR`, resuming the very stall being measured.

`psutil` is never required, only ever an upgrade: `pip install
pytest-failure-instrumentation[psutil]`.

## Status

Working and tested on Linux: worker deaths (every verdict above), internal
errors, the run summary, attribution, severity, fingerprinting, the payload
models and their round-trip, and the platform probes.

Not yet wired into the engine: stall detection and collection-mismatch diffing.
Both have a tested implementation and neither has a model in the union yet — a
member with no producer would promise a payload that never arrives. The Windows
and macOS probe paths are written from the platform APIs and have not been
executed on those systems.
