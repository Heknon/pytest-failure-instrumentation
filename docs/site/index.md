---
title: "Failure instrumentation"
description: "pytest-failure-instrumentation is a pytest plugin that records why pytest workers die, stall or disagree, and serves a run's live worker view to Sahara."
pageType: landing
owner: "morphine"
sidebar:
  label: Overview
  order: 0
---

pytest-failure-instrumentation records the failures that happen outside a
test's call phase (a killed worker, a wedged run, workers that collected
different tests, an error inside pytest itself) and turns each into one
structured incident that names whose code is responsible. It also serves a
running pytest session's workers and live call stacks over HTTP, which is what
Sahara UI's Workers view shows.

## Where to start

- [Turn on failure instrumentation](/failure-instrumentation/how-to/turn-it-on/)
- [Serve live stacks from a container](/failure-instrumentation/how-to/serve-live-stacks/)
- [Incidents](/failure-instrumentation/concepts/incidents/): what one says and how severity is decided.
- Reference: [Settings](/failure-instrumentation/reference/settings/) and [Verdicts](/failure-instrumentation/reference/verdicts/).
- In Sahara UI: [Set up the live worker view on GitLab CI](/sahara/ui/how-to/set-up-the-live-worker-view/).
