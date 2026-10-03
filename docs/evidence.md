# FND-04 evidence and reproducibility

Evidence belongs to this prototype's original issue criteria, not to full R06/R07
product acceptance. Source review/test/merge are separate states. The final
integrator records exact reviewed head and remote merge/PR outside the
implementation's self-referential commit.

The [machine receipt](../evidence/provenance.json) binds the implementation
commit, file digests, commands, retained runtime manifests and actual binary
hashes. [Acceptance output](../evidence/acceptance.log) names the real tests;
[acceptance summary](../evidence/acceptance.json) reports their actual outcomes.
[Reference output](../evidence/reference.log) is independent pure-model evidence,
not a replacement for SQL execution. [Benchmark data](../evidence/benchmark.json)
contains individual branch times, relation sizes, WAL growth, query/edit samples,
reconcile/accept/post times, row counts and host conditions. The supported
reproduction commands are in README.

## Failure and repair history

All native runs used retained owned PostgreSQL/PostGIS, never mocked engines.
The local `.runs` receipts and `/tmp/fnd04-*.log` logs are retained on the task
runner; selected summaries are committed here.

1. Empty-schema baseline: 20 database tests failed as expected because the
   prototype schema was absent. This checks that acceptance requires actual SQL.
2. SQL installation error: an unparenthesized CASE inside a PL/pgSQL IF prevented
   function creation. Corrected before accepting any real result.
3. The supposed disjoint-field test fixture also changed the numeric field on
   both sides. Both model and database correctly classified a conflict; the
   fixture was corrected to express the intended disjoint change.
4. Initial 100k workload finished. The first 1m/10-branch workload completed
   branch copies but accept spent more than two minutes in a DELETE anti-join;
   that one job-owned query was explicitly canceled. This is a partial run.
   Filtering unchanged rows before UPSERT helped the small workload, but was not
   proof of the slow DELETE's cause.
5. A fresh rerun exhausted the initial `/tmp` data filesystem during its fourth
   1m branch, despite available project-disk capacity. The harness now allocates
   data/WAL in its evidence filesystem and keeps only sockets in `/tmp`.
   Previous stopped synthetic clusters were removed after matching their
   job-ownership markers and runtime receipts, checking PostgreSQL version and
   absence of a live postmaster PID. Logs and cleanup receipts remain. No user
   or shared database was touched.
6. The [actual EXPLAIN comparison](../evidence/queryplan.json) found stale
   statistics estimated one branch row and selected nested-loop anti-join;
   ANALYZE changed the estimate to about 500k layer rows and chose hash anti-join.
   The diagnostic accept finished in 1.42 seconds. Prepare now refreshes the
   current/snapshot statistics before accept locks, and includes that cost in
   its measured time. This diagnostic was one branch, not the final ten-branch
   benchmark; it used the same relevant query forms before the independent
   review's selector checks were added.
7. Separate-context reviewer `/root/fnd03_identity` reproduced SQL NULL-selector
   fallthrough: NULL resolution silently chose base and NULL edit operation
   inserted. Explicit NULL rejection and regression tests now preserve rows,
   heads, unresolved conflicts and unsealed candidates. Review must independently
   verify the final corrected head; the implementer's statement is not approval.

## Interpretation boundaries

Measurements use one synthetic two-layer PointZ group on the recorded Linux
runner. They include psql connection overhead, use local warm caches, and do not
exercise catalog policy or a production service. Other work can share the host.
Percentiles derive from 30 query and 20 edit samples, not sustained load. The
query retrieves/counts up to 1,000 feature IDs under a bbox condition; it does
not serialize full feature or geometry payloads and is not the product API p95.
Branch time is bounded eager copying; storage is visibly proportional to rows
and branch count. Post timing is the full transaction, an upper bound for its
target-head lock duration, not separately instrumented lock-only time.

No restore-cost measurement, raster/polygon workload, M-geometry support,
Windows runtime test, user evaluation, full permission lifecycle, release test
or product quota follows from this report. Those requirements remain unchanged.
Backend termination shows transaction rollback and retry, not full database
crash, backup/restore or P4 recovery acceptance.
