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


## Live resource collection

Run the opt-in resource comparison independently of other test workloads:

```sh
python benchmarks/resource_cost.py --workers 80 --cases 2400 --pairs 2 --output resource-cost.json
```

It alternates baseline and resources-enabled runs with the live server enabled
in both, using real xdist processes and temporary file I/O. The report includes
elapsed time, test-duration median/p99, shutdown cost, sample duration/errors,
and observed process coverage. Missing worker samples or failed resource cleanup
fail the command. The 120-second added test-p99 bound is a synthetic backstop,
not proof of customer-suite p99 behaviour. File-inventory costs should be tested
separately with representative configured roots; the benchmark does not enable
recursive directory inventories. Do not add this 80-worker job to every commit.
