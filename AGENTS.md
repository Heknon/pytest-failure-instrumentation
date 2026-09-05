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
   affected platform. Put the failed pytest node ID in `tests`; use one node ID
   per line if several tests failed.
4. If the unchanged commit may simply have encountered runner noise, use
   GitHub's native **Re-run failed jobs** instead of creating a new commit.

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
