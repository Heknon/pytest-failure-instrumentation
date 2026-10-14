---
title: "Turn on failure instrumentation"
description: "Install pytest-failure-instrumentation and switch it on for a pytest suite, so worker deaths, stalls and internal errors become incidents."
pageType: how-to
owner: "morphine"
---

pytest-failure-instrumentation does nothing until a run asks for it. Do this
once per suite.

## Before you start

- Python 3.9 or later and pytest 7.0 or later.
- pip can reach the internal package index.

## Steps

1. Install it in the environment the tests run in:

   ```bash
   pip install pytest-failure-instrumentation
   ```

   py-spy, which reads live stacks, is installed with it.
2. Switch it on for every run, and name your own packages, so a failing frame
   in your code is told from one in a dependency or in tests:

   ```ini
   [pytest]
   addopts = --failure-instrumentation
   failure_packages = <your top-level packages>
   failure_product_version = <your product's version>
   ```

   For a single run, pass `--failure-instrumentation` instead.
3. To receive incidents as they happen, implement the hook in `conftest.py`
   or your own plugin:

   ```python
   def pytest_failure_incident(incident):
       ...  # incident.model_dump() is the incident as a dict; str(incident) is its alert text
   ```

## Check it worked

After a run, `.pytest-failures/` holds a directory for the run, with a
`.state` and `.events` file per worker (`main` without pytest-xdist). Without
the hook, this directory is where the evidence stays.

To turn it off for one run, pass `-p no:failure_instrumentation`.
See [Settings](/failure-instrumentation/reference/settings/) for every option.
