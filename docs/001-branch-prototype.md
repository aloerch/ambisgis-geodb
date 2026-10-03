# ADR 001: typed eager snapshots for the branch prototype

Status: FND-04 engineering choice, pending independent review and merge. This
does not authorize production quotas or close the later DB/P4 tasks.

The controlling platform plan chapter 03 requires one owned geodatabase schema,
typed feature storage, service-controlled edits, stable UUID identity, isolated
named branches, explicit accept before post, and atomic multi-layer promotion.
FND-02 is accepted; its retained owned database build is reused without changing
the source tuple. The standing owner delegation covers routine engineering and
independent review, while preserving actual acceptance criteria.

## Alternatives assessed

The following is a documentation and architecture comparison, accessed
2026-10-02. No alternative was installed or benchmarked and no compatibility,
performance or maintenance-quality conclusion is implied.

| Approach | Documented behavior | Fit and remaining integration work |
| --- | --- | --- |
| Kart | Git-style branches, commits and explicit conflict resolution; PostGIS working copies occupy a schema that Kart manages and external applications edit. | Useful interchange/offline design. Adopting the working-copy authority would require replacing its edit governance and integrating exact-head accept/post with catalog policy and atomic domain validation. Retain as an optional future interchange candidate. |
| GeoGig | Feature/attribute conflict handling and explicit resolution; PostgreSQL backend persists repository objects, predominantly as key/value data, without requiring PostGIS. | Useful reference for spatial merge behavior. Its object authority is a different storage model from the required typed domain tables; integration would need a proven single-authority projection and post transaction. No evidence here justifies that additional complexity. |
| QGIS versioning plugin | Historized PostgreSQL layers and checkout/commit workflows, including Spatialite offline copies; the original repository points to a GitLab successor. | Useful desktop workflow reference. The managed service must own authorization and mutations independently of a plugin, so direct use would require redesigning the write boundary and post lifecycle. No plugin-only dependency is adopted. |
| Owned typed snapshots | Prototype materializes a typed immutable base and typed current branch rows in a bounded repeatable-read transaction; SQL constraints and locks control merge acceptance/post. | Directly tests the required isolation and transaction model with the selected owned engine. Storage is proportional to rows × branches; measured cost and a full correctness suite must precede any production quota. |

Kart sources: [working copies](https://docs.kartproject.org/en/latest/pages/wc_types/postgis_wc.html)
and [branch/conflict commands](https://docs.kartproject.org/en/latest/pages/command_reference.html).
GeoGig sources: [merge semantics](https://geogig.org/docs/repo/merging.html)
and [storage backend](https://geogig.org/docs/repo/storage.html).
Plugin sources: [project successor](https://gitlab.com/Oslandia/qgis/qgis-versioning),
[original repository workflow](https://github.com/Oslandia/qgis-versioning), and
[historical documentation](https://qgis-versioning.readthedocs.io/en/latest/)
(documentation identifies its generation date as 2016; this is not a current
runtime compatibility test).

## Decision and consequences

Keep the plan's eager typed snapshot approach for the owned prototype. Reuse the
owned PostgreSQL/PostGIS implementation, without adding another mutating
repository authority. Keep the pure oracle and explicit domain operations stable
when later optimizing storage. Typed payloads remain in per-dataset relational
tables; transient JSON carries canonical comparisons/conflicts only. Geometry
comparison includes EWKB SRID, byte order and Z; geometry is one atomic field.

Creation and reconcile use repeatable read. The branch base and current rows are
published atomically; creation takes a schema exclusion lock without locking the
DEFAULT row against ordinary edits. Prepare captures exact source/target heads
and seals a candidate. Accept changes the branch only. Post locks both versions
in UUID order, rejects stale heads/generation, promotes changed rows, and commits
the target/source checkpoints plus events/outbox/idempotency together. A durable
candidate becomes the new base by reference.

Prepare refreshes current/snapshot relation statistics before accept acquires
version locks. A recorded million-row EXPLAIN diagnostic found the old statistics
estimated one branch row and selected a nested-loop anti-join; refreshed
statistics estimated about 500,000 rows in that layer and chose a hash anti-join.
The diagnostic accept then completed in 1.42 seconds. This is a measured reason
for the refresh, whose cost is included in reconcile timings, not an unrecorded
manual tuning prerequisite. The final ten-branch measurement remains separate.

The implementation still scans full candidate tables to identify the changed
rows during accept/post. Consequently the post transaction time grows with total
rows even for one change; it is not a constant-cost delta index. The measurements
must make this limitation visible. Production implementation needs bounded
work, explicit delta indexing and quotas supported by actual workloads; it may
adopt shared immutable snapshots if the same oracle/race suite passes.

Final corrected-code measurements, with ten active branches in each workload:

| Group rows / two layers | Branch creation median (range) | Relation bytes before / after ten branches | Reconcile / accept / post seconds |
| --- | --- | --- | --- |
| 100,000 | 1.63 s (1.50–1.80) | 37,740,544 / 737,828,864 | 3.088 / 0.147 / 0.144 |
| 1,000,000 | 24.21 s (17.33–26.43) | 373,571,584 / 7,325,302,784 | 27.265 / 1.570 / 1.769 |

Branch-creation WAL growth was 1,039,916,176 and 13,078,642,408 bytes respectively.
Post changed one feature after 20 sequential edits of that same branch feature.
Reconcile/accept still inspect full states. These outcomes support eager copies
as the correctness prototype, while visibly exposing proportional storage and
nonconstant post cost. They do not select a production quota. The real DB suite
must remain an invariant gate for later delta/shared-snapshot optimization.
See [complete evidence and conditions](evidence.md), including smaller query/edit
samples and all interrupted/failed attempts.

No new donor code, fork, package, source successor, workflow or release is
introduced by this choice. Further schema, relationship, policy, retention and
recovery work remains mandatory as listed in the prototype contract.
