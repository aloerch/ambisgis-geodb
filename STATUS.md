# FND-04 working status

Repository: `aloerch/ambisgis-geodb`, ID `1376927417`.
Task: [FND-04 / issue #1](https://github.com/aloerch/ambisgis-geodb/issues/1).
Branch: `fnd-04/typed-prototype`, started from verified `main`
`41fbb62b9967fc3409aa60a960823b7747f76523`.
Authority: [standing owner delegation](https://github.com/aloerch/ambisgis-platform/blob/ambisgis/main/plan/authorizations/2026-10-02-standing-delegation.md).
Claim: [automation record](https://github.com/aloerch/ambisgis-geodb/issues/1#issuecomment-5966058477).

Implemented locally: typed two-dataset eager snapshot prototype; independent
pure reference model; real-DB acceptance harness; snapshot/time measurement
runner; documentation comparison and engineering decision. No remote branch,
workflow, source-fork, default or release change was made by the implementer.

Implementation/tested code: `2fff211ba954ac9f2ab0955a73cb80f258042e93`.
Actual results: 25 real PostgreSQL/PostGIS tests passed in 18.592 seconds with
zero skips; 12 independent pure-reference examples passed. The database suite
includes 35 seeded merge comparisons, simultaneous competing posts, backend
termination/rollback/retry, concurrent snapshot creation, stale heads/plans,
geometry Z conflicts, typed constraints, explicit resolution, deny-by-default
access and NULL-selector regressions identified by separate-context review.

Both required measurement workloads completed: 100,000 and 1,000,000 group rows,
two typed datasets, ten active branches. Median branch creation was 1.63 and
24.21 seconds; group relation storage after ten branches was 737,828,864 and
7,325,302,784 bytes (19.55× / 19.61× the respective initial schema sizes).
One-feature post transactions took 0.144 / 1.769 seconds. These are recorded
synthetic development measurements, not production quotas or service claims.
See [bound evidence and failure history](docs/evidence.md).

Independent reviewer `/root/fnd03_identity` reproduced a NULL-selector correctness
bug; the corrected implementation and regression tests are now available for
final independent verification. Acceptance remains pending the integrator's
final review record, required checks and actual PR merge. No PR or merged/released
state is claimed by this local implementation. All test clusters are stopped.

This task is the P0 prototype and design decision only. Full DB/P4 schema,
authorization, history, recovery and end-user branch workflows remain mandatory;
see [the explicit remaining work](docs/prototype-contract.md). FND-02 remains
accepted. Next dependency-ready task selection belongs to the live Project and
the integrator, after this task's actual merge evidence is reconciled.
