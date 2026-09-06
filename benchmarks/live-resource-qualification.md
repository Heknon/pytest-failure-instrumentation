# Live resource qualification — 2026-09-06

## Local resource stress check

Linux 6.18.35, Python 3.12.13, pytest 9.1.1, xdist 3.8.0, psutil 7.2.2.
Container memory limit: 20 GiB; CPU quota: 8 cores. The visible CPU count is 9.

Command:

```sh
python benchmarks/resource_cost.py --workers 32 --cases 2400 --pairs 2 --worker-mb 256 --scan-files 10000 --sample-seconds 1 --output resource-stress.json
```

Each worker touched a 256 MiB allocation and retained it throughout its tests.
Both modes used the existing callstack server. Enabled runs also inventoried
10,000 pre-existing files while the tests created 2,400 more.

| Run order | Resources | Elapsed (s) | Call p99 (s) | Median worker RSS at fixture setup (MiB) | Samples | Collector errors | History deleted |
|---|---|---:|---:|---:|---:|---:|---|
| 1 | False | 52.350 | 0.508416 | 298.95 | 0 | 0 | True |
| 2 | True | 51.835 | 0.508895 | 298.94 | 51 | 0 | True |
| 3 | True | 52.863 | 0.507893 | 298.94 | 52 | 0 | True |
| 4 | False | 53.595 | 0.509024 | 298.94 | 0 | 0 | True |

Both enabled runs observed all 32 workers plus the controller. Maximum sampled
summed RSS was 10,105,286,656 bytes (9.41 GiB; shared pages can be counted twice).
Maximum individual collection pass was 169 ms; this excludes history writes
and the filesystem helper. Both enabled runs completed repeat directory scans
with new-file counts. No missing samples or errors were reported by the collector.

This checks a production-sized per-worker allocation, not a saturated machine
or every customer workload. CPU and memory were not deliberately exhausted.
Local lint and small regression commands overlapped parts of the comparison;
differences of this size should be treated as noise, not a speed improvement.
RSS snapshots are not continuous per-worker peak measurements. No Windows
memory-performance claim follows from this Linux result.

## Fault-injection and concurrency coverage

The regression suite covers inactive-run rejection despite failed disk writes
and deletion, cross-process and concurrent-reader lease semantics, retrying
cleanup for completed embedded runs, unpublished data, partial disk writes,
committed-record corruption, rotation under two readers, database-full rollback,
zombie-owner helper exit, and preservation of cumulative counters and capacity
gauges. The normal native smoke suite includes these checks.

Full CI/portability status belongs to the PR checks for the exact head commit;
this local report does not waive those release gates.
