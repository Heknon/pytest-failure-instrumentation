# Profiling examples

A suite whose every module is one thing the profiler is meant to flag, with
the finding each one expects in its docstring. `demo_product` stands in for
the product under test and is named in `failure_packages`, so the findings
carry `owner=product` the way they would for yours.

```console
cd examples/profiling
pytest                     # one process
pytest -n 2 --dist loadfile   # two workers: adds the imbalance finding
```

The findings print in the terminal summary and land in `incidents.jsonl`
through the `pytest_failure_incident` hook in `tests/conftest.py`. A
speedscope flame graph for every test a finding names is written under
`.pytest-failures/run-*/profiles/`.
