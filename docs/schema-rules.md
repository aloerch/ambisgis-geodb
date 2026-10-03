# DB-02 schema rules and migration decision

DB-02 extends the DB-01 managed registry, typed tables, identity mapping and
write primitive. It adds no separate catalog, public service, policy authority
or named-branch implementation. Source and runtime custody remain the accepted
owned PostgreSQL/PostGIS builds. Migration `001_managed.sql` is unchanged.

## Versioned definitions

A definition with `schema_version:2` has the v1 `fields` and `geometry`, plus
explicit `subtypes` (null or selector definition), `relationships` and `rules`
arrays. Fields accept optional `default` (a typed literal) and `domain`:

- Coded: `{"kind":"coded","values":["new","old"]}`.
- Inclusive range: `{"kind":"range","min":0,"max":20}`.

Literals follow DB-01 encoding: int32 JSON integers, int64/decimal strings,
ISO dates, explicitly zoned timestamps, booleans and UUID strings. No expression
strings are interpreted. Decimal precision reaches the existing 38-digit limit.
Coded values are non-null, unique under their declared type; nullable fields
still accept explicit null. Numeric/date/timestamp ranges reject inverted bounds.

Subtypes have `{"field":"kind","variants":[{"code":1,"defaults":{},"domains":{}}]}`.
The selector is a nonnullable int32 or string. Codes are unique and required.
Overrides name existing fields and cannot change the selector. Subtype domains
replace a base field domain for that subtype. A selector field default chooses
a subtype when omitted; explicit null never selects a default.

Defaults apply only to missing values in newly inserted rows. A subtype override
takes precedence over the base default. Explicit null remains null, subject to
nullability/rules. Updates retain the full-row DB-01 contract: omission fails
instead of resetting stored values. Changing subtype on an update requires a
complete row that satisfies the new subtype's domains. Definitions reject
ill-typed defaults and defaults outside their effective domain.

Canonical encoding is `ambisgis-typed-schema-json-v2`: the same sorted-key,
UTF-8, no-float JSON algorithm, with the new properties included. V1 bytes and
fingerprints are unchanged. The outer managed-dataset-v1 descriptor keeps its
existing identity fields; its explicit definition version and canonical encoding
identify v2. No platform shared schema is silently changed.

## Finite validation rules

The initial deterministic subset contains only:

- `{"name":"present","kind":"required","field":"status"}`.
- `{"name":"limit","kind":"compare","field":"amount","operator":"le","operand":{"field":"limit_value"}}`.
- The same comparison with `"operand":{"literal":10}`.

Operators are `eq/ne/lt/le/gt/ge`. Both field operands must have the same type.
String/boolean/UUID comparisons support only equality/inequality, avoiding
collation-dependent ordering. Comparisons with a null operand fail; nullable
domain fields without a required/comparison rule can remain null.

The schema compiler produces real PostgreSQL CHECK constraints using validated
identifiers, enumerated operators/types and escaped typed literals. It never
evaluates supplied SQL, Python, Arcade, regex, code or network requests. Unknown
keys/operators/operands fail closed. Calculation rules, generated actor/time,
spatial expressions and field-editability policy are not advertised by this
increment; later capabilities require explicit definitions and tests.

## Relationships and atomic writes

Relationships are child fields referencing the target feature UUID:

```json
{"name":"inspection_parent","field":"parent","target_dataset":"<dataset UUID>",
 "cardinality":"many-to-one","on_delete":"restrict"}
```

Both datasets must belong to the same managed version group. The child field
is UUID; its nullability controls whether a parent is mandatory. `one-to-one`
adds uniqueness of the non-null parent reference. Foreign keys include
`version_id`, so a feature in a different version cannot satisfy the relation.
Only restrict-on-final-state deletion is supported; there is no cascading delete
or cross-group relationship. Internally this uses deferred PostgreSQL NO ACTION,
allowing a parent and its children to be removed in one atomic group operation.

`apply_group(connection, descriptor, edits)` accepts 1–128 unique dataset edits,
at most 10,000 rows total. Each edit is exactly `dataset_id/rows/replace`, using
the existing full-row shapes. All datasets must be in the descriptor's version
group. `apply_rows` preserves its signature and delegates a one-dataset request.

Group writes lock group metadata, then the DEFAULT head, and reject a stale
expected head. Schema changes use the same lock order. Defaults are applied
after locking the authoritative schema. Foreign keys/one-to-one uniqueness are
deferred during the group operation, then explicitly checked before the SQL
function returns. A failing call aborts all rows, identity mappings and head
changes. Sequence gaps on rollback retain DB-01 semantics. Each supplied upsert
receives a fresh feature revision; only the final head becomes visible at commit.

Both Python entry points require dedicated idle transactional connections and
commit only on success. SQL backend callers must also use a dedicated bounded
transaction, set transaction timeouts/timezone, and commit before reporting
success; a function return does not itself commit. Only constraints on the locked version group's registered dataset tables
are deferred/checked; other groups' constraint timing is untouched. Their exact
per-dataset constraint prefix is matched literally. Managed relation constraints
in this group are immediate on successful return. Neither operation is a field-patch or
idempotency-journal API. Catalog authorization still precedes backend invocation.

## Privileges, migration and retained revisions

Only `apply_rows` and `apply_group` may be granted to the selected trusted
backend. Raw storage, registry, DDL, identity sequence and schema-revision changes
remain inaccessible. The original apply_rows function OID keeps existing
legitimate grants during migration; the new raw helper has no backend/PUBLIC
EXECUTE grant. The grants checker includes both permitted entry points and
retains all DB-01 ownership/membership/bypass checks.

`revise_schema(connection, descriptor, definition)` is a migrator-only operation
for domains/defaults/subtypes/rules/relationships. It requires the exact prior
schema revision and group head, and rejects physical field/geometry changes.
It validates replacement constraints against all existing typed rows inside
one transaction. Success appends an immutable descriptor, selects it as active,
and advances schema generation/head without altering feature UUID/ObjectID
mappings. Failure restores all prior constraints, descriptors and head.
Identical definitions are a no-op after concurrency checks. Old revisions remain
in the immutable registry for historical descriptor reads; this does not create
historical feature snapshots. Groups with more than the sole DEFAULT version
are refused pending a later explicit branch migration design.

Forward migration 002 adds the v2 encoding, permits the trusted migrator to
select a retained active revision, and installs the guarded group operation.
There is no down migration or automatic prototype adoption. Before applying to
a populated environment take a verified backup, rehearse restore, apply ordered
checksum-locked migrations and verify retained identities/grants. Restore the
verified pre-migration backup in an isolated environment if migration cannot
succeed; never discard post-backup edits. The acceptance fixture performs a real
pg_dump/restore of populated v1 followed by forward migration and a managed
write. It is bounded DB-02 migration evidence, not full product recovery or
deployment qualification.

## Verification and remaining acceptance

Run the existing managed acceptance command with `--schema-rules` for DB-02,
and without it for DB-01 regressions. Source snapshots, owned-runtime hashes,
database logs and stopped synthetic clusters are retained in each fresh output
directory. `tests/test_schema.py` additionally runs without database dependencies
in the existing reviewed hosted check; no workflow changes are needed.

The DB-02 real suite tests defaults/null/subtypes/domains, typed/literal rules,
relationship cardinality/reordering/restrict, cross-group rejection, whole-group
rollback, failure before function return, schema revision retention, actual
schema/edit lock contention, service isolation, v1 grants/identity preservation,
backup/restore and exact decimal/date constraints. Existing DB-01 tests and the
independent FND-04 prototype remain regression gates. Independent review and
normal integrator merge are required before acceptance. DB-03+ branch lifecycle,
history, reconcile/post, API authorization, larger-volume quotas, full product
restore, installation and release remain required later work.
