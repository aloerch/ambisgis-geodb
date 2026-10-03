# DB-01 implementation evidence

The local tested implementation is `f3fb30ba844c4098c4544e097c582495b516bb22`,
based on the accepted FND04 merge `b7e5888b11a4b3d2246daeffe0173113f1628f2d`.
[DB-01 issue #2](https://github.com/aloerch/ambisgis-geodb/issues/2) was claimed by
the integrator after actual FND06 acceptance. Its original acceptance remains
stable identities under reorder/republication and real typed database
geometry/SRID/null/length constraints. [The contract and implementation notes](managed-schema.md)
describe the full foundation and its limits.

| Actual run | Outcome |
| --- | --- |
| `acceptance-001` | Failed: 17 UnicodeEncodeError errors in 19 tests because the inherited prototype fixture initialized SQL_ASCII. Database stopped; exact source bytes and a clearly labeled post-run diagnosis retained. |
| `acceptance-002` | 19 tests passed after explicit UTF8 initialization and migration encoding guard. |
| `acceptance-003` | Initial review checkpoint passed 22 tests. Independent review subsequently found a backend-role privilege precondition gap. |
| `acceptance-004` | First role repair failed closed with six WrongObjectType errors: PostgreSQL evaluated a sequence privilege function before filtering non-sequences. Exact failed source/logs retained. |
| `acceptance-005` | Ownership and subtype guards passed 24 tests; server-capability membership negatives were then added. |
| `acceptance-006` | Final 25 tests passed: 22 real database methods and 3 pure schema/identity guards; zero failures/errors/skips, 15.870 seconds, unchanged source, stopped database. |
| `baseline-004` | Final exact-source expected failure with ordinary setup omitted: 15 schema-absence errors, zero skips, stopped database. Earlier baseline attempts are retained. Tests that explicitly exercise migration installation can still install their fixture; this is a failing suite, not a claim that every case lacks a schema. |
| `fnd04-regression-001` | All unchanged 25 real database tests passed, zero skips, 28.314 seconds. |
| `reference-001` | All unchanged 12 pure FND04 oracle examples passed. |

Real tests cover transactional initial/forward migration failure, checksum
mismatch, reapply and two simultaneously blocked migration callers; UTF8 and
prototype/unmarked-schema refusal; caller transaction preservation; stable IDs
under shuffled replacement, deletion/reintroduction and namespace reuse;
concurrent allocation with stale-head rejection and explicit retry; full-batch
rollback; exact decimal/Unicode length; every scalar type; native large IDs and
int32 overflow; DEFAULT/group foreign keys; immutable mappings/schema revisions;
actual typed column/export inspection; permission denial and temporary-name
shadowing; direct/column/inherited privileges, owners with revoked ACLs, and
server capability memberships that do not exercise file/program operations. Geometry checks include 4326/2230 in XY/XYZ/XYM/XYZM, wrong type/SRID,
NULL/empty distinction, invalid polygons and nonfinite X/Y/Z/M coordinates.

The [public hash inventory](../evidence/db01/evidence.json) binds actual receipts,
commands, logs, source snapshots and the retained owned runtime. The
[actual synthetic descriptor](../evidence/db01/managed-export.json) is copied
byte-for-byte from the database test. PostgreSQL 15.19 and PostGIS 3.5.7 use the
same owned source identities as FND04. The runner verifies 72 retained runtime
artifacts before execution, records its Python path and creates only fresh
job-owned clusters with private Unix sockets and no TCP listeners. It does not
fetch/install packages. Evidence and stopped data are retained under
`/home/revelberry/Projects/AmbisGIS/build-worktrees/db01`.

Independent review reproduced the original helper accepting preexisting direct
nonspatial UPDATE and owner membership. The repairs inspect every reachable role,
including NOINHERIT/SET ROLE, explicit ownership independent of ACL revocation,
and server-defined pg_* capabilities before granting the finite write function.
They reject unsafe roles without revoking existing rights. The subsequent
WrongObjectType failure was corrected with subtype-safe CASE expressions. No
historical failure or original receipt was relabeled.

The reviewer independently re-probed the final frozen source: twelve actual
security cases passed, including repeated grant/real write for a clean role,
column and inherited privileges, owners with revoked ACLs, and dangerous server
capability memberships without exercising file/program operations. It rehashed
all 15 final source snapshots, 72 runtime artifacts and UTF8/migration bindings.
The original finding and final probe receipts are separately bound in the public
inventory. Final review of this reporting commit remains an integrator gate.

The final runtime receipt explicitly identifies managed migration hashes and
UTF8; the raw inherited fixture receipt remains separately retained. Its
prototype SQL hash is a fixture source reference, not the managed schema.

The original FND04 SQL, prototype implementation, tests and prior evidence are
unchanged. This increment requires separate-context migration/security review,
trusted exact-source database status and normal protected PR merge. A local
passing suite does not mark DB01 Merged or qualify full editing, named branches,
catalog authorization, restore, production operation or release. No remote
mutation was performed by the implementer.
