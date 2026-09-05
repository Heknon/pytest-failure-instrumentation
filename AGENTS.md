# CI policy for coding agents

CI cost matters in this repository. macOS runners are substantially more
expensive than Linux runners, and the complete suite deliberately contains
slow subprocess and timeout scenarios. Follow this policy when changing code.

## Normal pull-request validation

`.github/workflows/ci.yml` is the required per-commit gate. It runs the full
suite on Linux and a focused platform-facing smoke suite on current macOS and
Windows. Do not expand the macOS or Windows PR matrix merely to investigate a
single failure.

## Validating a CI fix

When a specific macOS or Windows test fails:

1. Diagnose the failure from the failed job log before changing code.
2. Commit the fix to the pull-request branch.
3. Dispatch the `Portability` workflow on that exact branch and choose the
   affected platform. Put the failed workflow's numeric run ID in
   `failed-run-id`; CI downloads that platform's seven-day `failed-tests-*`
   artifact and runs those node IDs only. The `tests` input remains available
   for an explicit node ID that did not come from CI.
4. If the unchanged commit may simply have encountered runner noise, use
   GitHub's native **Re-run failed jobs** instead of creating a new commit.

### AI-triggered reruns

An agent with GitHub access should normally use the PR comment interface after
it pushes a fix:

```text
/ci-rerun-failed run=33954455785 platform=macos python=3.13
```

Use the numeric ID of the failed source run, the platform that failed, and the
same Python version as its artifact. `.github/workflows/rerun-failed.yml`
accepts the command only on a pull request and only from a repository owner,
member or collaborator. Before starting the paid runner it verifies the exact
command grammar, confirms that the source run failed, confirms that its
platform-specific artifact exists and has not expired, and resolves the
current PR head SHA. The target job checks out that SHA without persisting Git
credentials and runs only the recorded node IDs.

After posting the command, poll the `Rerun failed tests` workflow rather than
posting the command again. Read its job status and logs until it completes. If
it fails, use the new run's `failed-tests-*` artifact for the next fix. Do not
trigger duplicate paid jobs while one is queued or running.

The comment workflow is defined on the default branch, as required for an
`issue_comment` trigger. It becomes available after this CI tooling is merged;
until then, use the reduced per-commit platform smoke jobs or GitHub's native
failed-job rerun. The manual `Portability` interface also becomes dispatchable
once its workflow definition exists on the default branch.

For local iteration, run `pytest --lf` to execute only failures remembered in
`.pytest_cache`, or `pytest --ff` to put remembered failures first and then run
the rest. Do not delete `.pytest_cache` while investigating a failure.

A targeted run proves only that the named regression is fixed. It is not a
replacement for the normal PR gate or the full portability gate, and its check
name must never be configured as the sole required merge check.

## Before merge or release

Apply the `full-portability` label to the pull request. Both complete non-Linux
suites must pass on the resulting run. After this workflow has landed on the
default branch, manually dispatching `Portability` with `platform=all` and a
blank `tests` input is equivalent. The scheduled weekly run provides drift
detection between changes; it does not waive this pre-merge gate. Release
workflows retain their own complete package matrix.

Prefer the smallest targeted rerun while iterating, then pay for one complete
cross-platform confirmation when the branch is actually ready to merge.

## Profiling release qualification

The `full-portability` label also starts the separate `Profile readiness`
workflow. It measures two alternating baseline/profile pairs with 80 Linux
workers and 2,400 synthetic I/O-heavy tests per run, and runs the existing
overhead gate plus native CPU attribution probes on Windows/Python 3.12.
This is intentionally on demand; do not add it to every-commit CI. Inspect
both workflows and their preserved JSON/log artifacts before signing off.

The 80-worker qualification budgets are at most 20% median elapsed overhead,
10 GiB summed process RSS, and 30 seconds in controller session-finish hooks.
Summed RSS double-counts shared pages and is sampled every half second. These
are synthetic qualification thresholds, not application performance promises.
Native probes require 70–130% of independently timed test-thread CPU to be
attributed to its actual Python caller, for sustained and single native calls
both holding and releasing the GIL. Failed single-call attribution must be
reported as a limitation; do not force sampler ticks or loosen assertions to
make a production claim pass. Allocation tracing remains separately opt-in
and is outside these runtime budgets.

## Targeted profiling iteration

When the user requests targeted-only iteration, apply `profiling-iterate` to
the PR. Routine CI then runs lint/type checks only; `Profiling iteration`
runs the selected macOS regression from its recorded failure artifact and
the Windows sampler/analysis tests plus profiler benchmarks. It uses the
same safe node-ID runner as the general failed-test toolkit. The comment
dispatcher requires its definition on master; this PR-label entry point
works before that tooling has been merged.

Do not apply `full-portability` or rerun a complete failed job while this
mode is active. Targeted greens are regression evidence, not a substitute
for a full release gate. Report any unverified full-suite coverage explicitly.
Remove the iteration label only when a full gate is wanted; the user's
request to avoid another sweep takes precedence over the default policy.
