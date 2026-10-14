---
title: "Verdicts"
description: "Every verdict a pytest-failure-instrumentation incident can carry for a dead worker, a stalled worker or a collection mismatch, and what tells each apart."
pageType: reference
owner: "morphine"
---

The verdicts pytest-failure-instrumentation gives the incidents about workers.
For what an incident is, see [Incidents](/failure-instrumentation/concepts/incidents/).

## A worker died (`worker_death`)

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
| `SELF_EXIT` | a non-signal POSIX exit code, `0` included; an unwitnessed Windows exit remains `UNKNOWN` because an external caller can choose the same code |
| `PROBABLY_SIGNALLED` | exit code 128–191, a wrapper ate the signal |
| `RUN_STOPPED` | a run found dead afterwards whose controller had been sent SIGTERM before this process's last heartbeat |
| `UNKNOWN` | no status obtainable (remote gateway) |

## A worker stalled (`worker_stall`)

| Verdict | Heartbeat | CPU | Means |
|---|---|---|---|
| `STALLED_BLOCKED` | alive | none | the test thread is waiting on something that is not coming |
| `STALLED_FROZEN` | stopped | — | native code is holding the GIL, or the process is stopped |
| `STALLED_SILENT` | never ran | — | the watchdog is off, so there is no passive evidence either way |
| *(not reported)* | alive | burning | slow, not stuck |

## Workers collected different tests (`collection_mismatch`)

| Verdict | Means |
|---|---|
| `COLLECTION_MEMBERSHIP_DIFFERS` | a test exists on one machine and not another |
| `COLLECTION_ORDER_DIFFERS` | same tests, different sequence — fatal too, since xdist addresses tests by position |
| `COLLECTION_PARAMETERS_UNSTABLE` | same tests, different *parameter values* — a parametrize that is not deterministic |

## Other kinds

| Kind | Verdicts |
| --- | --- |
| `internal_error` | `INTERNAL_ERROR` |
| `stack_server_unavailable` | `PORT_TAKEN`, `BIND_REFUSED` |
| `cpu_hotspot` | `PYTHON_CODE`, `LIBRARY_CALL`, `BACKGROUND_THREAD`, `GC_PRESSURE`, `NATIVE_THREADS` |
| `cpu_burst` | `LONG_BURST`, `RECURRING_BURST`, `BACKGROUND_BURST`, `CONTENDED` |
| `memory_profile` | `RETAINED_AFTER_TEST`, `TRANSIENT_PEAK`, `PEAK_OVER_CEILING`, `STEADY_GROWTH`, `WORKER_IMBALANCE`, `ALLOCATOR_RETENTION`, `HEAP_NOT_RETURNED` |
