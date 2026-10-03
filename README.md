# ambisgis-geodb

Experimental FND-04 branch engine for AmbisGIS, covering R06/R07 foundation
acceptance. This is a runnable typed PostgreSQL/PostGIS prototype, not the managed
geodatabase product, a server, an installer, or Esri branch protocol support.

The prototype has two typed PointZ datasets in one atomic version group, eager
immutable bases, optimistic branch heads, a pure merge oracle, explicit
prepare/resolve/accept/post, deferred natural-key checks, transaction audit/outbox
records, and idempotent post. It runs against retained AmbisGIS-owned PostgreSQL
15.19 and PostGIS 3.5.7 builds. It downloads and installs no dependencies.

Run from this checkout using Python 3.11 or newer and the retained owned build:

```sh
python3 tools/run_acceptance.py \
  --prefix /home/revelberry/Projects/AmbisGIS/build-worktrees/postgis-slice/run-003/prefix \
  --evidence .runs/acceptance
python3 tools/benchmark.py \
  --prefix /home/revelberry/Projects/AmbisGIS/build-worktrees/postgis-slice/run-003/prefix \
  --evidence .runs/benchmark

# Dependency-free reference examples for hosted CI (not DB acceptance):
python3 -m unittest discover -s tests -p test_reference.py -v
```

Both commands create a fresh job-owned cluster in the evidence directory, with a
short private Unix socket under `/tmp` and no TCP listener. Database and WAL
storage use the evidence filesystem rather than potentially small tmpfs. Only
generated synthetic fixtures are loaded. The
cluster is stopped and retained with an ownership marker; no existing database
or source build is reset. Evidence records build identities, actual binary
hashes, schema hash, timings and errors. The `--baseline` acceptance option proves
the suite fails when the implementation is absent. Running database tests without
the harness fails explicitly; missing databases are never reported as skips.

Read [the implementation choice](docs/001-branch-prototype.md),
[prototype contracts and remaining work](docs/prototype-contract.md), and
[measured evidence](docs/evidence.md) and [STATUS](STATUS.md). SQL files are experimental bootstrap definitions for empty
disposable databases; they are not a supported upgrade migration. Do not grant
ordinary users or notebooks ownership of this schema.

New first-party source files carry GPL-3.0-or-later identifiers. No Kart, GeoGig,
or QGIS-versioning source is incorporated; the comparison uses their documented
designs. PostgreSQL/PostGIS builds retain their own notices and licensing.
