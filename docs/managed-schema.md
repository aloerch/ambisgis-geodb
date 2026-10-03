# Managed dataset foundation v1

DB-01 adds an authoritative schema library and transactional migrations for
managed datasets. Its native contract is owned here. The platform's accepted
FND06 query profile remains a separate, bounded consumer experiment. No service
registry, policy grant model, public edit API or named-branch workflow is added.

`managed.migrate.migrate(connection)` installs the checksummed ordered SQL in
`migrations/`. `managed.database.create_dataset` registers a dataset and creates
its real typed table and identity sequence. Both require a trusted schema
migrator and a dedicated idle transactional connection. `export_schema` emits
the versioned native descriptor. `apply_rows` invokes the finite service write
primitive; `grant_service` grants only that primitive to a deployment-selected
trusted backend role. Connections use the retained psycopg2 driver; these tools
never install packages or fetch dependencies.

## Identity and version foundation

A dataset has an immutable UUID namespace, managed name, catalog item UUID,
opaque catalog policy reference, version group UUID and active immutable schema
revision UUID. Catalog references are identifiers, not permissions. Service
UUIDs, layer UUIDs and numeric service layer IDs belong to the service registry;
reordering published layers cannot redefine this dataset namespace.

Each group has exactly one DEFAULT version UUID and an opaque UUID head token.
Adding a dataset to an existing group advances its schema generation and head.
Every current table already has `(version_id, fid)` as its primary key,
`version_group_id` with referential integrity, and a per-feature revision UUID.
Foreign group/version combinations fail in PostgreSQL. Named versions are
explicitly rejected in this increment. A later reviewed migration can add their
lifecycle without replacing these current-row keys. The head is a concurrency
checkpoint, not a claim of complete commit ancestry or historical snapshots.

The durable registry maps `(dataset_id, fid)` uniquely to a positive signed
64-bit ObjectID. UUIDs must be supplied explicitly; callers must retain imported
UUIDs (or a future explicit external-key mapping) across imports. Missing UUIDs,
duplicate UUIDs within a batch are rejected. The library never generates identity
from row order; callers must not supply a changing row-position surrogate as a
UUID. The same UUID in two datasets has two independent namespaces.

A per-dataset PostgreSQL sequence allocates new integer identities. The registry
survives deletion, empty replacement, reorder and republication. Reintroducing a
UUID reuses its original mapping; another UUID never receives that ObjectID.
Rollback may leave sequence gaps, which carry no semantics. There is no reset,
recycling or registry-delete service operation. Native identity exports encode
ObjectIDs as decimal strings, including values beyond JavaScript's exact-number
range. The separate `object_id_int32` compatibility projection rejects overflow;
it never narrows or renumbers native identities.

## Typed definition and export

The exact v1 definition envelope is:

```json
{
  "schema_version": 1,
  "fields": [
    {"name": "name", "type": "string", "nullable": true, "max_length": 8},
    {"name": "amount", "type": "decimal", "nullable": false, "precision": 12, "scale": 3}
  ],
  "geometry": {"type": "Point", "srid": 4326, "dimensions": "XY", "nullable": false, "allow_empty": false, "require_valid": true}
}
```

Every property shown for its type is explicit; unknown keys, floats used as
bounds, missing/null substitutes, duplicate names and reserved identity/system
column names fail validation. Field names use lowercase ASCII identifiers.
Definitions preserve field order. Nonspatial tables use explicit `geometry:null`.
Supported scalar types are string, int32, int64, exact decimal, boolean, UUID,
date and zoned timestamp. Geometry types are Point, LineString, Polygon and
their Multi equivalents, with XY, XYZ, XYM or XYZM dimensions. SRIDs must exist
in the retained PostGIS spatial reference catalog. The tests exercise 4326 and
2230; this is not a transformation engine or universal CRS qualification.

`ambisgis-typed-schema-json-v1` canonicalization validates and normalizes this
exact envelope, encodes JSON as UTF-8 with sorted object keys, fixed `,`/`:`
separators, preserved field order and no floating-point values. SHA-256 covers
only these immutable typed-definition bytes. Dataset/catalog IDs, policy,
managed name, group membership and mutable head do not enter that hash. The
database checks canonical text against JSON and its SHA-256, and blocks schema
revision updates/deletion. There is no mutable second schema authority.

`export_schema` returns `contract:"ambisgis-managed-dataset-v1"`, version 1,
dataset/name/catalog/policy IDs, group/DEFAULT/schema revision UUIDs,
`schema_sha256`, `canonical_encoding`, `definition`, `group_schema_generation`,
identity field metadata and `{version_id,head_revision}` under `current`.
Identity metadata explicitly names `fid`, `object_id`, `feature_revision` and
`version_id`, the int64/string ObjectID encoding and non-recycling guarantee.
The real acceptance run retains `managed-export.json` with an actual descriptor,
feature identity and PostgreSQL column types. API01 must consume this authority;
the fixed FND06 layer profile is not promoted into a durable dataset registry.

## Lossless database constraints

Typed values occupy physical PostgreSQL columns, not a feature JSON blob.
JSONB is transient full-row input and immutable schema metadata. Integer, UUID,
boolean, date and timestamp columns use their native types. String columns use
text plus character-length checks; no bounded-varchar cast can silently truncate
them. Nullable text distinguishes NULL from an empty string. Exact decimal
columns use numeric plus explicit finite, scale and precision checks, avoiding
numeric typmod's rounding before a constraint can inspect the value. Trailing
zeroes that do not change the value are accepted. Nonfinite numeric values fail.

Geometry columns use PostGIS geometry with exact type/SRID/ZM, validity,
emptiness, nullability and finite-coordinate constraints. This intentionally
avoids geometry typmod's implicit SRID assignment for SRID=0. Inputs require
explicit EWKT SRID; no automatic transform, force-2D or SRID reassignment occurs.
Z and M finite checks supplement PostGIS's two-dimensional validity predicate.
Invalid polygons and NaN/Infinity in any coordinate fail. Empty and NULL geometry
acceptance are independently explicit. Geometry remains typed and GiST-indexed.

Managed databases require UTF-8. Date inputs are ISO dates; timestamps require
an explicit RFC3339 zone and at most six fractional digits, matching PostgreSQL's
microsecond resolution without rounding. Stored timestamps represent instants;
their original textual timezone spelling is not retained. Native decimal and
int64 input values use decimal strings. The SQL primitive independently checks
these types rather than trusting only a Python caller.

## Transaction and privilege boundary

`apply_rows(dataset,version,expected_head,rows,replace)` accepts complete rows,
up to 10,000 per transaction. All defined fields must be present, including
explicit NULLs. `replace=false` upserts; `replace=true` additionally removes
current rows absent from the supplied full set while retaining their identity
mappings. The cap is a finite primitive limit, not a qualified production quota
or large publication staging protocol. Future publication must preserve these
identities and supply an explicit staging/activation saga.

The function locks/checks the actual DEFAULT head before allocating identities,
then atomically changes typed rows, mappings and the head. Invalid later rows
roll back earlier rows and mappings. Per-feature tokens advance on every supplied
upsert, including equivalent values; no semantic no-op optimization is claimed.
Two callers using the same head cannot both commit; the loser receives
`STALE_HEAD` and must deliberately refresh/retry. Idempotency/request journals,
field patches, edit history and complete branch workflows remain later tasks.

PUBLIC receives no managed schema, table, sequence or function grants. End users,
notebooks, QGIS and render clients never receive the trusted backend or schema
owner role. The granted backend can execute only the fixed write primitive;
direct table writes, identity updates, sequence reset and managed DDL fail.
Before adding grants, the helper refuses existing effective managed table or
column mutation grants, sequence allocation/reset, schema creation, other
managed function execution and privileged/owner memberships. It checks every
role reachable through membership, including SET ROLE with NOINHERIT. It refuses
unsafe preexisting roles rather than revoking unrelated grants. Deployment must
keep role provisioning and subsequent membership/grant changes controlled;
this check cannot prevent a trusted administrator from granting new powers later.
Render roles can receive SELECT on an approved view. The security-definer
function fixes its search path with `pg_temp` last and qualifies registry,
PostGIS and generated physical table references. Actual tests try temporary-name
shadowing and privilege bypass. A database superuser/schema owner remains trusted
and can administer its own objects; this is not a claim to sandbox that owner.

Live catalog authorization must precede service invocation. Opaque policy IDs
do not implement that check, row/field restrictions, or permission revocation.
DB01 grants are backend isolation tests, not an additional policy authority.

## Migration and recovery contract

The runner takes a transaction-scoped advisory lock, verifies the ordered
filename/checksum history, requires owned PostGIS in `public`, and applies all
pending DDL/history entries in one transaction. Successful reapply is a no-op;
changed or unknown migration history fails. Lock/statement budgets are finite.
Failure leaves no partial initial schema or partial forward migration. The
actual tests force simultaneous lock waits and prove one installation, apply a
forward migration twice, and inject failing DDL. Existing caller transactions
are rejected rather than committed by surprise.

An existing FND04 `geodb` schema or unmarked `managed` schema is explicitly
rejected. The experimental SQL and its accepted evidence remain unchanged.
There is no automatic prototype adoption, source-table drop or destructive down
migration. Never edit an already applied migration: repair with a newly reviewed
successor. Production deployment must take a verified backup/checkpoint and
exercise restore/migration on staging before applying changes. Complete product
backup/restore qualification remains its own task; transactional DDL rollback in
this fixture is not represented as that qualification.

## Reproduction and remaining gates

Use the retained owned runtime and Python driver:

```sh
/home/revelberry/Projects/AmbisGIS/build-worktrees/geonode-role-propagation/run-003/venv/bin/python -B tools/run_managed_acceptance.py \
  --prefix /home/revelberry/Projects/AmbisGIS/build-worktrees/postgis-slice/run-003/prefix \
  --runtime-evidence /home/revelberry/Projects/AmbisGIS/build-worktrees/postgis-slice/run-003-evidence-final.json \
  --evidence /path/to/new/disposable/run
```

The runner refuses an existing output directory, verifies 72 retained runtime
artifacts, records actual sources, copies their bytes, uses a private Unix socket
with no TCP listener, runs real SQL and stops the job-owned cluster. Its files
are synthetic and retained. `--baseline` deliberately omits normal setup so
schema-dependent tests fail; it is not a passing acceptance mode. The old FND04
suite remains an independent regression gate. Independent migration/security
review and normal protected merge remain required. DB02 rules/domains/subtypes,
later relationships/attachments, API authorization, named branches, restore and
production operational qualification are not silently claimed here.
