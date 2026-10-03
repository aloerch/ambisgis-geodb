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

Acceptance is pending final measured evidence, separate-context review and merge.
Initial empty-schema baseline failed as intended. An early 20-test real-database
suite passed before additional concurrency/delta tests. The first large workload
completed ten 1m-row branch copies, but was interrupted during its expensive
accept operation after exposing unnecessary unchanged-row UPSERT work. The
filtered-delta repair and complete acceptance/benchmark rerun are in progress.
Failures and partial runs are retained locally, not presented as passes.

This task is the P0 prototype and design decision only. Full DB/P4 schema,
authorization, history, recovery and end-user branch workflows remain mandatory;
see [the explicit remaining work](docs/prototype-contract.md). FND-02 remains
accepted. Next dependency-ready task selection belongs to the live Project and
the integrator, after this task's actual merge evidence is reconciled.
