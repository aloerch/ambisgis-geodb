# DB-03 typed snapshots and branch lifecycle

DB-03 extends the existing managed registry with backend-only named branches,
typed immutable bases and bounded creation jobs. It uses the same dataset,
version group, schema revision and feature identity authority as DB-01/DB-02.
Migration 003 is append-only; migrations 001/002 and the managed-dataset-v1
export contract are unchanged. There is no prototype adoption.

## Snapshot and identity contract

A branch UUID is its existing managed version UUID. The authoritative live
name remains `managed.version.name`; branch metadata adds owner, visibility,
editor policy references, lifecycle state, metadata revision and operation ID.
Owner/visibility/editor references are descriptive inputs for the authoritative
catalog policy layer. They confer no access by themselves. These functions are
trusted backend interfaces, not an end-user authorization API; API-04 remains
required.

Reservation durably creates a private `creating` record with a caller-supplied
branch UUID and operation UUID. It captures no source head or schema. Replay
of the same operation and canonical request returns the original record; a
different request conflicts. The later copy transaction captures DEFAULT's
head and group schema generation under one REPEATABLE READ snapshot.

Each dataset has a typed snapshot table with the same scalar/geometry types,
nullability and generated CHECK constraints as current storage. The internal
snapshot key is `_snapshot_id`, outside the existing user field grammar;
a legitimate user field named `snapshot_id` remains valid and hash-sensitive.
Snapshot primary/unique keys and relationships include the snapshot UUID.
Stable feature UUID/ObjectID mappings are reused, with no new allocation or
recycling. Branch current tables retain version-scoped relationships.

Creation copies every layer into a private immutable base and the branch's
typed current rows, checks final relationship/uniqueness constraints, compares
source/base/current counts and row hashes, seals the base, appends one immutable
creation commit, advances the branch head and activates the branch in one
transaction. No partial active branch can become visible. The immutable
manifest pins each exact schema revision, schema fingerprint, row count,
logical payload bytes and digest. This creation commit is an anchor only;
full edit history, audit/outbox and edit idempotency belong to DB-04.

Canonical payloads use PostgreSQL JSONB scalar rendering under fixed UTC and
ISO/YMD settings, with exact XDR EWKB geometry encoded as hexadecimal. They
exclude only internal version/snapshot keys and replace geometry's default
rendering with XDR. A dataset digest is SHA-256 of the schema fingerprint,
a colon, and the concatenated hexadecimal SHA-256 row digests ordered by
feature UUID. This encoding is pinned to the owned runtime, not an advertised
cross-database interchange format. Source, current and snapshot measurement
use the same payload. Sealed rows, manifests, snapshot metadata and creation
commits reject mutation; backend roles have no raw table privileges.

## Concurrency and transaction envelope

All managed schema mutations take the group row FOR UPDATE and advance
`schema_generation`. Dataset creation, schema revision and migration 003
obey this protocol. Internal snapshot installers run only inside those
schema-owner transactions; they are not standalone migration APIs.
Retained sealed snapshots and completed branches block schema changes.
An empty creating/failed reservation does not pin a schema.

DEFAULT writes take the group row FOR KEY SHARE, then the DEFAULT version row
FOR UPDATE. A copy takes the group FOR SHARE as its first data read, then quota,
then its own version and branch metadata. DEFAULT never locks the copy's quota
row, and snapshot source identity references the immutable group/default pair,
not the mutable DEFAULT version row. DEFAULT edits can commit while a copy is
between layers; the copy retains its earlier head and consistent cross-layer
rows. An already uncommitted DEFAULT writer also does not block the copy.

The outer SELECT establishes the REPEATABLE READ snapshot before a waiting
row lock can finish. Therefore every schema mutation also changes the guarded
group tuple: if DDL commits while the copy waits, PostgreSQL raises 40001
instead of exposing an old descriptor with a new physical schema. The worker
retries the whole transaction, preserving the caller's expected metadata
revision. No descriptor/head substitution is used.

`managed.branches.populate_branch` requires an idle dedicated connection.
It sets REPEATABLE READ and LOCAL statement/lock deadlines before its first
SELECT; these SET statements do not capture a data snapshot. Each attempt is
bounded by at most 30 seconds statement timeout and 5 seconds lock timeout,
with at most three attempts and retries only for 40001. Callers may choose
shorter bounds. These limits bound the invocation, not merely subsequent
statements inside the function. Each Python operation commits before returning
success. SQL callers holding trusted backend credentials must use the same
dedicated bounded transaction envelope and commit before reporting success;
SQL function return alone is not commit. There is no public unbounded SQL API.

## Quotas and retention

The schema owner must configure finite quotas before reservation. All ten
keys are required:

| Limit | Accounting or bound |
|---|---|
| max_branches | Creating, active, archived and deletion-pending records; at most 100 |
| max_current_rows / max_snapshot_rows | Retained named-branch current/base rows; each at most 1,000,000 |
| max_current_bytes / max_snapshot_bytes | Retained uncompressed canonical UTF-8 payload bytes; each at most 1 GiB |
| max_source_rows / max_source_bytes | Finite source preflight bounds for one creation |
| max_history_rows | Retained creation anchors; at most 1,000,000 |
| max_attachment_bytes | Must be zero; attachments are not implemented here |
| max_idle_seconds | Maximum age of an empty creation reservation; at most one year |

All nonattachment limits are positive. `max_idle_seconds` checks reservation
age before population; it does not promise active-branch idle cleanup or
automatic garbage collection. The worker can explicitly recover expired
reservations. Source row count is checked across the group before expensive
canonical byte construction. Source bytes are checked before digest aggregation
or any copy. Byte accounting counts the canonical uncompressed logical payload,
including text and XDR geometry, not compressed TOAST size or physical database
capacity. It does not include indexes, page overhead, WAL, metadata or DEFAULT
storage, and must not be presented as a blanket database quota.

Named-branch operations lock group, quota, then version. Every operation that
changes counted state also updates the quota revision tuple, so stale
REPEATABLE READ waiters fail with 40001 instead of overbooking against old
counts. Quota reconfiguration cannot reduce limits below retained usage.
Copy jobs in a group serialize on quota coordination; DEFAULT writes remain
independent. Logical deletion keeps both typed copies, manifests and creation
anchors accounted. Physical garbage collection and operational capacity
management are later work.

## Lifecycle, failure and recovery

Normal get/list return only active or archived records. Operational get, for
trusted workers, may return creating, failed, deletion-pending or deleted state.
The finite transitions are:

- Creating to active only through atomic successful population.
- Creating to creation_failed through cancellation, copy failure or explicit
  recovery using the expected metadata revision.
- Active to archived, or active/archived to deletion_pending.
- Deletion_pending to deleted after retention-reference checks.
- Empty creation_failed to deleted for terminal cleanup and public name reuse.

A failed copy rolls back all rows, manifests, snapshot metadata, commits and
quota usage. `create_branch` then attempts to mark the durable empty reservation
failed; a lost connection can prevent this cleanup. Replay of the stable
operation resolves that reservation, and a fresh worker uses `recover_branch`
with the expected revision before terminal cleanup. Cancellation/recovery
cannot overwrite a newer metadata revision. Data-free failed records consume
no branch slot/storage and do not block schema changes.

Lost successful responses replay the same active branch, base and commit,
with no second copy. Deleted operation replay still returns the original
deleted identity even after another operation reuses its old public name.
Terminal cleanup preserves the last public name only as retired metadata;
the underlying version name becomes exactly `_deleted_<its UUID without dashes>`.
User reserve/update calls reject underscore names, and a deferred integrity
guard permits this exact internal alias only in deleted state.

Publication/history/recovery snapshot references prevent lifecycle deletion.
Logical deletion releases the live-branch reference but retains all sealed
data; retained snapshots continue to block incompatible schema changes.
No physical garbage collection or restoration of a deleted branch is claimed.
Name, visibility and editor references can be updated with an exact metadata
revision while active. Owner transfer is not implemented.

## Privileges, migration and acceptance

`grant_service` retains the two DEFAULT write routines. The separate
`grant_branch_worker` grants only nine finite branch routines, after the
existing ownership/membership/bypass checks. Neither grant permits raw managed
table reads/writes, DDL, sequence mutation, quota configuration or internal
snapshot helper execution. Catalog authorization must precede these backend
calls. Public execution is revoked, definer paths are fixed and deferred
version integrity checks run with the required owner privileges.

Before applying ordered checksum-locked migrations to a populated environment,
take and verify a backup and rehearse isolated restore. The real DB-03 fixture
dumps/restores a populated DB-02 database, applies 003, verifies legacy field
names and stable identities, and creates a branch. Existing DB-01/DB-02
forward-migration and privilege tests remain required regressions. There is no
down migration or permission to discard later edits when restoring a backup.

Run `tools/run_managed_acceptance.py --branches` with the owned prefix and
runtime receipt; omit the selector for DB-01 and use `--schema-rules` for DB-02.
The tests include real cross-layer writes during copy, a writer ahead of the
snapshot, waiting DDL/40001/retry, quota races, statement timeout, cancellation
at three copy phases, backend termination, recovery, replay, retained quotas,
byte boundaries and finite backend privileges. Evidence preserves failures,
source snapshots, logs, runtime hashes and stopped synthetic clusters.

Named-branch edits remain unavailable until DB-04. Reconcile, post, full edit
history, end-user policy enforcement, attachments, garbage collection, complete
product recovery and release acceptance remain required later work. Separate
integrator review and protected merge are pending for this candidate.
