# Profiler production gate

Run `python benchmarks/profile_gate.py` from an environment with the checkout
installed. It compares median wall time for the same fixed-CPU workload under
plain pytest, failure instrumentation, and failure profiling. It also repeats
hotspot detection five times, checks five quiet runs for false-positive CPU
findings, and measures profiling with four xdist workers.

The gate intentionally excludes allocation profiling: `tracemalloc` is a
deep diagnostic with workload-dependent cost, not an always-on mode with a
small overhead promise.

The budgets are regression limits, not universal performance guarantees:

- instrumentation: at most 1.05x baseline wall time;
- serial profiling: at most 1.10x;
- four-worker profiling: at most 1.12x;
- the known sustained hotspot must be detected in all five trials.
- all five quiet trials must remain free of CPU findings.

The CI result includes every raw timing and median in its JSON output. Change
a budget only with measurements that explain why the product contract changed.
