"""Where a run's CPU and memory went, attributed to the code that spent it.

Everything else in this package waits for something to go wrong. This waits
for nothing: switched on, it samples the process that runs the tests for the
whole of the run and, at the end, names the functions that burnt the CPU and
the tests that kept the memory - as incidents, through the same hook as a
crash, because "your image comparison is 38% of the run" is a finding a
reader wants flagged in exactly the way a segfault is.

Two halves, in two processes, the way the rest of the plugin is split:

* :mod:`.sampler` runs on whichever process runs the tests. A thread of its
  own wakes every few milliseconds, reads every thread's stack and every
  thread's CPU counter, and charges the CPU each thread burnt since the last
  wake to the stack it is in now. Idle threads weigh nothing, which is the
  difference between a profile of where the *time* went and one of where the
  *cores* went - and only the second answers "why is this worker at 30%".
  What it saw is written per test to ``<worker>.profile.jsonl``.

* :mod:`.analysis` runs on the controller once the run is over, is pure, and
  is where every threshold lives. It folds the workers' records together,
  charges each stack to the first frame that belongs to somebody (see
  :mod:`..analysis.attribution`), and turns what crosses a threshold into a
  finding. Nothing in it reads a clock or a file, so it is tested against
  records built by hand.

Off by default. Nothing here runs until ``--profile`` or ``failure_profile``
asks for it, because it is the one thing in this package with a running cost
on a healthy run - about one percent of a core per worker.
"""
