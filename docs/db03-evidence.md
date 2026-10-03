# DB-03 execution evidence

Repository `aloerch/ambisgis-geodb` (ID `1376927417`), issue
[#4](https://github.com/aloerch/ambisgis-geodb/issues/4), branch
`db-03/typed-snapshots`, accepted DB-02 base
`c963e2baae1901935b25b0d436e988b47e153eb8`.

The clean tested implementation is
`e47001431d903363bb8d84979eb1e12b396ff916`.
The [machine receipt](../evidence/db03/evidence.json) binds 22 exact source files,
72 rehashed owned runtime artifacts, commands, logs, source snapshots and
cleanup observations. The evidence/documentation follow-up changes no tested
implementation. Migrations 001/002 and the reviewed workflow are byte-identical
to the accepted base.

| Final actual run | Result | Elapsed receipt time |
|---|---|---|
| DB-03 real database, `db03-final-001` | 20/20 pass, zero skips | 9.67 s |
| DB-01: 22 database + 6 schema, `db01-final-001` | 28/28 pass, zero skips | 9.52 s |
| DB-02 real database, `db02-final-001` | 12/12 pass, zero skips | 6.04 s |
| Retained FND-04 real database, `prototype-final-001` | 25/25 pass, zero skips | 22.85 s |

The same Python 3.12 environment also passed the 12 dependency-free reference
examples and the separate six-case schema check. These pure checks are distinct
from database acceptance; the six schema cases also appear in the DB-01 run.

All three final managed inputs record the clean implementation head and
identical source manifests, with source unchanged throughout each run. The
prototype and pure controls record that same clean head; their unchanged
source bytes match the managed manifests and Git blobs. Final assembly rehashed
all source files against the tested commit and reverified all 72 runtime
artifacts. This is implementer execution evidence. The independent exact-head
review is pending with the integrator; no self-review or product release
acceptance is claimed.

## Coverage that determines this increment

The 20 DB-03 methods exercise:

- One real parent/child group edit committing while population is paused
  between layer copies, with the earlier captured head and old typed values
  preserved across both layers. A separate writer-ahead case holds an actual
  uncommitted DEFAULT edit while the copy completes.
- A real schema mutation committing while the RR copy waits on the group,
  producing 40001; bounded whole-transaction retry then captures the new
  schema generation. Creating reservations do not incorrectly pin schema.
- Two RR branch-count/configuration races and concurrent copy quota contention,
  with finite count/storage accounting and no DEFAULT wait on copy quota.
- Cancellation before first copy, between layers and before sealing; exact
  fixture backend termination; actual statement deadline; rollback of all
  rows/manifests/commits/accounting and explicit reservation recovery.
- Lost-response operation replay, changed-request conflict, metadata CAS,
  finite archive/delete transitions, external retention references, failed
  terminal cleanup/name reuse and old-operation identity preservation.
- Independent logical payload byte calculation for Unicode/quoted text and
  XYZM geometry; exact-bound success, one-byte-over failure, compressed text
  rejection and retained deleted usage. Generated CHECK clauses are preserved.
- Populated DB-02 pg_dump/restore/forward migration, legacy `snapshot_id`
  user-field compatibility and an independent hash-sensitivity assertion.
- Finite branch worker grants, search-path shadowing, ordinary bypass denial,
  snapshot immutability and refusal of named-branch edits.

The retained DB-01/DB-02 suites continue to cover migration checksums/rollback,
typed storage, stable identities, finite validation and same-group constraints,
unrelated-group isolation, exact decimal rules and DEFAULT service grants.

## Preserved failing attempts

The initial five-case DB-03 baseline ran against the implemented DB-02 schema.
Its `baseline:false` flag means migrations were enabled; the older runner's
`--baseline` would omit all migrations and is not the intended dependency test.
Actual failures discriminate the missing branch functions/catalog and the old
exclusive group lock blocking a DEFAULT writer.

| Actual run | Preserved result |
|---|---|
| db03-baseline-001 | 5 cases: 1 failure, 4 errors |
| db03-dev-001 | Initial 5 cases pass |
| db03-review-baseline-001 | 8 cases: 2 failures, 2 errors |
| db03-review-baseline-002 | 8 cases: 2 failures, 2 errors |
| db03-dev-002 | Expanded 9 cases pass |
| db01-regression-001 | 28 cases: 1 error |
| db02-regression-001 | 12 cases: 1 error |
| db03-dev-003 | 19 cases: 1 failure |
| db03-dev-004 | 20 cases pass |
| db01-regression-002 / db02-regression-002 | 28 / 12 cases pass |

The first expanded baseline reproduced RR quota overbooking/configuration
races and writer-ahead blocking. Its between-layer case had a fixture schema
missing the required `require_valid` property; that fixture was corrected
before the second baseline. The second baseline reproduced both actual
DEFAULT-writer timeouts as well as both quota races.

The quota row had been locked without modification, allowing an RR waiter to
retain its stale count. Count-changing operations now update a revision tuple.
The snapshot's foreign key had acquired a key-share lock on the mutable DEFAULT
version row; it now references the immutable group/default identity, preserving
the captured head as an immutable value without blocking DEFAULT head updates.

The first DB-01/DB-02 regressions exposed a deferred integrity trigger running
without registry-read privilege at commit for the restricted service role.
The trigger now has a fixed qualified definer context; both suites pass with
their original service-role behavior preserved.

The 19-case run showed that 100,000 highly compressible text bytes incorrectly
passed a 2,048-byte preflight when measured using compressed tuple size. The
repair counts the same canonical uncompressed UTF-8 payload used for integrity
hashes, checks row bounds first, then byte bounds before hashes or copying.
The additional exact-bound test verifies both geometry/text and retained usage.

No failed receipt is rewritten as success. Every managed run retains its
original source snapshot, including intermediate dirty implementation states.
The content-addressed public artifacts deduplicate identical bytes while
preserving every original private path and hash. Synthetic SQL backup files
remain private with hashes in the receipt. All 15 database clusters are retained
stopped; source/build custody and reproducibility inputs remain intact.

## Environment, commands and cleanup

Owned PostgreSQL source: `2ff1375b5dd8bf09d8cb0e795974528180fd75ca`.
Owned PostGIS source: `9816f82458db774e62906cfb2c4f01f8b262c862`.
No dependency was downloaded and no existing database was reset. Each run
used a fresh owned cluster, synthetic records, a private Unix socket and no TCP
listener. The existing host bridge was used because the application namespace
could not launch the fixture directly.

```sh
/home/revelberry/Projects/AmbisGIS/build-worktrees/geonode-role-propagation/run-003/venv/bin/python -B tools/run_managed_acceptance.py \
  --branches \
  --prefix /home/revelberry/Projects/AmbisGIS/build-worktrees/postgis-slice/run-003/prefix \
  --runtime-evidence /home/revelberry/Projects/AmbisGIS/build-worktrees/postgis-slice/run-003-evidence-final.json \
  --evidence /path/to/new/db03-run
```

Omit `--branches` for DB-01/schema or replace it with `--schema-rules` for DB-02.
The separate prototype command uses `tools/run_acceptance.py --prefix <same>
--evidence <fresh directory>`. Exact executed argument arrays and logs,
including prototype/pure output, are retained in the machine receipt.

Cleanup verifies each cluster has no postmaster PID, socket entries or matching
postgres process. The fixture retains an empty socket directory, which is
reported explicitly. An initial evidence-assembly assertion incorrectly
expected removal of that empty directory; the retained assembly diagnostic
records the correction. No running cluster or runtime failure was concealed.

The [branch contract](branch-snapshots.md) defines backend-only policy references,
reservation-age bounds, retained logical quota accounting, immutable schema
pins and recovery limits. Independent migration/security review, normal protected
merge and fresh postmerge evidence remain integrator gates. This worker made no
remote writes. DB-04+ edits/history, reconcile/post, end-user authorization,
physical GC, product restore, installation and release remain outstanding.
