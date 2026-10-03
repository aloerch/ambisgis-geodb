# Experimental SQL contract and remaining acceptance

The prototype's one version group contains `assets` and `observations`.
Each table stores feature UUID, unique name, nonnegative numeric(14,3), and a
non-null PointZ in EPSG:4326. This is an intentionally bounded synthetic schema,
not a general schema registry. UUID identifies the feature; no ObjectID mapping
or public REST surface is advertised.

The trusted worker starts a bounded `REPEATABLE READ` transaction for
`create_branch(name)` and `prepare_reconcile(branch_uuid)`. Other operations may
use read committed. The harness bounds subprocess and concurrency timeouts;
production lock/request limits remain to be designed.

| SQL operation | Result and safeguards |
| --- | --- |
| `edit(version, expected_head, dataset, operation, uuid, name, value, ewkt)` | Full-row insert/update/delete. Locks/checks branch head and enforces typed constraints. No field-patch or per-feature token API is implied. |
| `prepare_reconcile(branch)` | Captures target and builds a candidate without changing either current version. Existence then field merge; canonical EWKB is atomic. Combined natural-key violations abort the whole prepare transaction. |
| `resolve_conflict(plan, dataset, uuid, choice)` | Explicit whole-feature base/ours/theirs selection; final resolution seals candidate. Replacement editing and user-resolution audit UX are future work. |
| `accept_reconcile(plan)` | Checks heads/generation and all conflicts, applies candidate to branch, advances its base to captured target. DEFAULT is unchanged. |
| `post(plan, expected_source, expected_target, request_uuid)` | Rechecks exact accepted heads; applies both datasets atomically, advances checkpoints, records events/outbox and idempotent result. Reuse with a different payload or database principal fails. |

All functions are security invoker. PUBLIC has no schema/table/function grants.
The test-only schema owner is trusted and can administer or bypass its own
objects, like any database owner. The denied outsider tests establish absence of
PUBLIC write/function access; they are not catalog authorization tests. Do not
grant the owner role to users, render engines, notebooks, or desktop clients.
The final service must enforce catalog policy and permissions again at post.

Retained bases and candidates are immutable to the normal functions. There is no
garbage collector, destructive reset endpoint, nested branch or branch deletion
operation. `Cluster.reset()` acts only in its newly initialized synthetic test
database. Migration rollback consists of retaining or discarding that disposable
cluster; no user database migration is supplied or accepted.

Mandatory later work is unchanged: generic datasets/schema revisions; stable
integer ID mappings; rule/domain/subtype expressions; relations and attachments;
field/row authorization and permission revocation; per-feature tokens and edit
idempotency; auditable resolution revisions; complete append-only typed revision
history; schema migration/recovery; lifecycle/retention; cache policy; worker
crashes at every boundary; randomized operation sequences beyond merge; actual
backup/restore and successful post after restore; production quotas; native
service/UI/QGIS workflows; compatibility tests; upstream-disconnected product
rebuild/repair. Backend termination during post tests transaction rollback and
retry only; it is not a database crash/restore acceptance result.

Initial and intermediate failures are preserved in evidence: the empty-schema
baseline fails; the first SQL run exposed an unparenthesized CASE expression;
the second run caught a purported disjoint-field fixture accidentally changing
the same field on both sides. The SQL and fixture were corrected before the
passing run. No failure was reclassified as a skip.
