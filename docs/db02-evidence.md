# DB-02 local acceptance evidence

Issue [#3](https://github.com/aloerch/ambisgis-geodb/issues/3), repository
`aloerch/ambisgis-geodb` (ID `1376927417`), branch `db-02/schema-validation`.
Implementation base is accepted DB-01 merge
`ef9ef7364a7babc2ebcb4841c2c670633af59d59`; tested clean implementation head is
`5c3cf1e3f1d152bc5a0bffecc9eda6726449586e`.

The [machine receipt](../evidence/db02/evidence.json) binds each result to
source files, command arguments, logs, runtime artifacts and stopped synthetic
clusters. Final code was unchanged during both managed runs; all tested source
bytes still matched when the receipt was generated. This evidence/documentation
commit changes no tested implementation source. Independent review and normal
integrator merge remain pending. No remote write was made by this worker.

| Actual run | Outcome |
|---|---|
| DB-02 failing baseline on DB-01, `db02-baseline-001` | 4 errors at unsupported v2 schema; 0 skips; cluster stopped |
| First implementation, `db02-implementation-001` | 4/4 pass |
| Initial DB-01 regression, `db01-regression-001` | 2 failures and 1 error; preserved |
| Expanded DB-02, `db02-implementation-002` | 9/9 pass |
| Final DB-02, `db02-final-001` | 10/10 pass; 0 skips |
| Final DB-01 and schema, `db01-final-001` | 28/28 pass; 0 skips |
| Independent retained prototype, `prototype-final-001` | 25/25 real DB cases pass; 0 skips |
| Python 3.12.14 CI-equivalent, `ci-final-001` | 12 reference + 6 schema unit cases pass |

The initial regression failures were two hardcoded assumptions that only
migration 001 existed and an error-priority regression: cross-version writes
reported STALE_HEAD before DATASET_VERSION_MISMATCH. The tests now verify the
two installed immutable migrations and inject failure in migration 003; the
implementation again preserves the DB-01 error contract. These changes do not
remove any prior acceptance assertions.

A later dependency-free schema run caught Python Decimal context rounding in
`abs()` at the 38-digit precision boundary: two errors in six cases. The literal
validator now uses context-independent `copy_abs()`; subsequent unit and real
database checks verify the largest supported decimal without rounding. That
development invocation's output remains in the session transcript; the
structured retained final receipts bind the corrected source. An initial CLI
invocation failed before creating a database because the new suite flag had not
yet been installed in the harness; the corrected failing baseline is the first
DB-02 database run.

The baseline deliberately ran DB-02 acceptance against the implemented DB-01
base. Its `baseline:false` receipt refers to the older runner flag that omits
all migrations; it does not mean the DB-02 expectation passed. The two changed
baseline source files are retained alongside the receipt; all other baseline
source hashes refer to the exact DB-01 base. Failed artifacts are not rewritten
as successful evidence.

## Commands and local retained environment

From the component checkout, use the retained owned driver and binaries:

```sh
/home/revelberry/Projects/AmbisGIS/build-worktrees/geonode-role-propagation/run-003/venv/bin/python -B tools/run_managed_acceptance.py \
  --schema-rules \
  --prefix /home/revelberry/Projects/AmbisGIS/build-worktrees/postgis-slice/run-003/prefix \
  --runtime-evidence /home/revelberry/Projects/AmbisGIS/build-worktrees/postgis-slice/run-003-evidence-final.json \
  --evidence /path/to/new/db02-run
```

Omit `--schema-rules` for DB-01/schema regressions. The initial retained prototype
command is `python3 -B tools/run_acceptance.py --prefix <same owned prefix>
--evidence <fresh directory>`. The actual commands, Python executable and
per-file inputs are in each managed `inputs.json`; this worker used the existing
host bridge because the application sandbox could not create its namespace.

No dependencies were downloaded. The harness verifies retained runtime artifact
hashes against the existing receipt before starting a private Unix-socket
cluster with no TCP listener. PostgreSQL source is
`2ff1375b5dd8bf09d8cb0e795974528180fd75ca`, PostGIS source is
`9816f82458db774e62906cfb2c4f01f8b262c862`. Database/log/dump/source-snapshot
directories remain under `.runs/`, with job ownership markers and no live
postmaster PID after each run. The DB-02 migration test actually dumps a populated
v1 database, restores into another job-owned database, applies 002, verifies
identities/descriptors/grants and successfully performs another managed write.
It never resets an existing user's database.

## Exact acceptance boundary

The [schema contract](schema-rules.md) maps the implemented behaviors and limits.
Positive and negative cases exercise coded/range domains, missing-only defaults,
subtype overrides, explicit null, finite field/literal rules, exact decimal/date
values, one-to-one and many-to-one constraints, final-state restrict deletion,
cross-group rejection, reordered atomic group edits, mapping/head rollback,
schema revision retention, actual schema/edit contention and least-privilege
entry points. The invalid relationship case proves the SQL function itself
raises before returning, not only when its caller commits.

The prototype remains a separate regression target; it is not adopted into the
managed schema. This increment does not claim complete branch snapshots,
edit/reconcile/post/history, public authorization, installation, source repair
qualification or a release. Package checks and independent review are separate
integrator evidence, not substitutes for these database tests.
