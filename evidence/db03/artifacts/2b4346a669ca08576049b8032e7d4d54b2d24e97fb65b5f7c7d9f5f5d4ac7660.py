# SPDX-License-Identifier: GPL-3.0-or-later
"""DB-03 acceptance on the owned PostgreSQL/PostGIS fixture; no mock database."""
import concurrent.futures
import copy
import json
import unittest
import uuid
import psycopg2
import managed.database as database
from test_managed_database import ManagedDatabaseTests, connect

LIMITS = {
    "max_branches": 4, "max_current_rows": 100, "max_snapshot_rows": 100,
    "max_current_bytes": 1048576, "max_snapshot_bytes": 1048576,
    "max_source_rows": 100, "max_source_bytes": 1048576,
    "max_history_rows": 100, "max_attachment_bytes": 0, "max_idle_seconds": 86400,
}

class BranchSnapshotDatabaseTests(ManagedDatabaseTests):
    def configure(self, d, **changes):
        limits = dict(LIMITS, **changes)
        self.query("SELECT managed.configure_branch_quotas(%s,%s::jsonb)",
                   (d["version_group_id"], json.dumps(limits)))
        return limits

    def reserve(self, d, *, branch=None, operation=None, name=None):
        branch = branch or str(uuid.uuid4())
        operation = operation or str(uuid.uuid4())
        name = name or "branch_" + uuid.uuid4().hex
        result = self.query(
            "SELECT managed.branch_reserve(%s,%s,%s,%s,%s,%s::uuid[],%s)",
            (d["version_group_id"], branch, name, str(uuid.uuid4()), "private", [], operation))[0][0]
        return result

    def populate(self, reservation, connection=None):
        conn = connection or self.connection
        with conn:
            with conn.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("SELECT managed.branch_populate(%s,%s)",
                               (reservation["branch_id"], reservation["revision"]))
                return cursor.fetchone()[0]

    def stored(self, d, version):
        return self.query('SELECT fid::text,object_id,feature_revision::text,name,amount::text,'
                          'encode(public.ST_AsEWKB(geom,\'XDR\'),\'hex\') FROM managed."'
                          + database.table(d["dataset_id"]) + '" WHERE version_id=%s ORDER BY fid', (version,))

    def test_default_writer_finishes_while_snapshot_schema_guard_is_held(self):
        d = self.dataset()
        guard = connect()
        try:
            with guard.cursor() as cursor:
                cursor.execute("SELECT schema_generation FROM managed.version_group WHERE group_id=%s FOR SHARE",
                               (d["version_group_id"],))
            def write_default():
                conn = connect()
                try:
                    return database.apply_rows(conn, d, [self.row()])
                finally:
                    conn.close()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(write_default)
                try:
                    self.assertIsNotNone(future.result(timeout=3))
                finally:
                    guard.rollback()
        finally:
            guard.close()

    def test_branch_captures_typed_group_base_and_later_default_edits_do_not_leak(self):
        first = self.dataset()
        second = self.dataset(version_group_id=first["version_group_id"])
        rows = [self.row(), self.row()]
        database.apply_group(self.connection, self.refresh(first), [
            {"dataset_id": first["dataset_id"], "rows": [rows[0]], "replace": False},
            {"dataset_id": second["dataset_id"], "rows": [rows[1]], "replace": False}])
        before = [self.stored(d, first["default_version_id"]) for d in (first, second)]
        self.configure(first)
        reservation = self.reserve(first)
        branch = self.populate(reservation)
        self.assertEqual(branch["state"], "active")
        self.assertEqual([self.stored(d, branch["branch_id"]) for d in (first, second)], before)
        for d in (first, second):
            database.apply_rows(self.connection, self.refresh(d), [], replace=True)
        self.assertEqual([self.stored(d, branch["branch_id"]) for d in (first, second)], before)
        self.assertEqual(self.query("SELECT count(*) FROM managed.snapshot_dataset WHERE snapshot_id=%s",
                                    (branch["base_snapshot_id"],)), [(2,)])
        self.assertEqual(self.query("SELECT state FROM managed.snapshot WHERE snapshot_id=%s",
                                    (branch["base_snapshot_id"],)), [("sealed",)])

    def test_creating_and_cancelled_branches_are_absent_from_normal_reads(self):
        d = self.dataset()
        self.configure(d)
        reservation = self.reserve(d)
        self.assertEqual(self.query("SELECT managed.branch_get(%s,false)", (reservation["branch_id"],)), [(None,)])
        failed = self.query("SELECT managed.branch_cancel(%s,%s)",
                            (reservation["branch_id"], reservation["revision"]))[0][0]
        self.assertEqual(failed["state"], "creation_failed")
        self.assertEqual(self.query("SELECT managed.branch_get(%s,false)", (reservation["branch_id"],)), [(None,)])
        self.assertEqual(self.query("SELECT count(*) FROM managed.snapshot WHERE branch_id=%s",
                                    (reservation["branch_id"],)), [(0,)])
        self.assertEqual(self.stored(d, reservation["branch_id"]), [])

    def test_named_branch_writes_stay_unavailable_until_edit_pipeline(self):
        d = self.dataset()
        database.apply_rows(self.connection, d, [self.row()])
        self.configure(d)
        branch = self.populate(self.reserve(d))
        descriptor = self.refresh(d)
        descriptor["current"] = {"version_id": branch["branch_id"], "head_revision": branch["head_revision"]}
        before = self.stored(d, branch["branch_id"])
        with self.assertRaisesRegex(psycopg2.Error, "DATASET_VERSION_MISMATCH|BRANCH_EDITS_UNAVAILABLE"):
            database.apply_rows(self.connection, descriptor, [self.row()])
        self.assertEqual(self.stored(d, branch["branch_id"]), before)

    def test_snapshot_catalog_contract_exists(self):
        for relation in ("branch", "snapshot", "snapshot_dataset", "commit", "branch_quota", "snapshot_reference"):
            self.assertIsNotNone(self.query("SELECT to_regclass(%s)", ("managed." + relation,))[0][0], relation)

def load_tests(loader, tests, pattern):
    return unittest.TestSuite(BranchSnapshotDatabaseTests(name)
                             for name in BranchSnapshotDatabaseTests.__dict__ if name.startswith("test_"))
