---
title: "Incidents"
description: "What a pytest-failure-instrumentation incident is, the kinds it raises, and how its owner, severity and confidence are decided."
pageType: explanation
owner: "morphine"
---

An incident is pytest-failure-instrumentation's account of one failure that
could not explain itself: a worker that died, a worker that stalled, workers
that collected different tests, or an error inside pytest's own machinery.
Each is a structured record (a pydantic model) with a **verdict** saying what
happened and an **owner** saying whose code is responsible.

## Kinds

| Kind | Raised on |
| --- | --- |
| `worker_death` | any run |
| `worker_stall` | any run |
| `collection_mismatch` | runs under pytest-xdist |
| `internal_error` | any run |
| `run_summary` | any run |

The live stack server raises `stack_server_unavailable`, and profiling
(`--failure-profile`) raises `cpu_hotspot`, `cpu_burst` and `memory_profile`.

Without pytest-xdist, the session itself is the worker, recorded as `main`.

## Owner and severity

Severity follows from the owner:

| Owner | Severity |
| --- | --- |
| `product` (a package in `failure_packages`) | critical |
| `third-party` | high |
| `customer-code`, `runtime` | informational |
| `unknown` | needs-triage |

`needs-triage` means somebody has to look, not that it is bad. Every incident
also carries a **confidence** (high, medium, low) and a **fingerprint** that
stays the same across runs, so the same failure can be counted.

## Why silence needs a heartbeat

pytest's controller hears from a worker only when a phase completes, so a
twenty-minute test and a deadlock look the same from outside. Each worker
writes a heartbeat carrying its CPU time; a stall verdict is reached from
those beats, and a stack is read only afterwards, because asking a wedged
process a question can change its answer.

The full list of verdicts is in [Verdicts](/failure-instrumentation/reference/verdicts/).
