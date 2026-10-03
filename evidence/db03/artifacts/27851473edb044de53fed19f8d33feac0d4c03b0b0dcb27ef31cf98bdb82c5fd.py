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

    def pause_copy(self, d, *, timing="BEFORE", event="INSERT"):
        import random
        key = random.randrange(1, 2000000000)
        name = "pause_" + uuid.uuid4().hex
        self.query('CREATE FUNCTION public."' + name + '"() RETURNS trigger LANGUAGE plpgsql AS $$'
                   ' BEGIN PERFORM pg_advisory_xact_lock(' + str(key) + '); RETURN NEW; END $$')
        target = "s_" + uuid.UUID(d["dataset_id"]).hex
        self.query('CREATE TRIGGER "' + name + '" ' + timing + ' ' + event +
                   ' ON managed."' + target + '" FOR EACH ROW EXECUTE FUNCTION public."' + name + '"()')
        guard = connect()
        with guard.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        return guard

    def test_actual_multilayer_copy_allows_atomic_default_edit_between_layers(self):
        import test_managed_database as fixture
        parent_definition = {"schema_version": 2, "fields": [
            {"name": "value", "type": "int32", "nullable": False}],
            "geometry": {"type": "Point", "srid": 4326, "dimensions": "XYZM",
                         "nullable": False, "allow_empty": False},
            "subtypes": None, "relationships": [], "rules": []}
        parent = self.dataset(parent_definition)
        child_definition = copy.deepcopy(parent_definition)
        child_definition["fields"].append({"name": "parent", "type": "uuid", "nullable": False})
        child_definition["relationships"] = [{"name": "parent_ref", "field": "parent",
            "target_dataset": parent["dataset_id"], "cardinality": "one-to-one", "on_delete": "restrict"}]
        child = self.dataset(child_definition, version_group_id=parent["version_group_id"])
        p = {"fid": str(uuid.uuid4()), "values": {"value": 1}, "geometry": "SRID=4326;POINT ZM(1 2 3 4)"}
        c = {"fid": str(uuid.uuid4()), "values": {"value": 1, "parent": p["fid"]}, "geometry": "SRID=4326;POINT ZM(5 6 7 8)"}
        edits = [{"dataset_id": parent["dataset_id"], "rows": [p], "replace": False},
                 {"dataset_id": child["dataset_id"], "rows": [c], "replace": False}]
        database.apply_group(self.connection, self.refresh(parent), edits)
        original = self.refresh(parent)
        identities = [self.mapping(d) for d in (parent, child)]
        self.configure(parent)
        reservation = self.reserve(parent)
        second = sorted([parent, child], key=lambda d: d["dataset_id"])[1]
        guard = self.pause_copy(second)
        worker = connect()
        with worker, worker.cursor() as cursor:
            cursor.execute("SET application_name='db03-between-layers'")
        changed = copy.deepcopy(edits)
        for edit in changed:
            edit["rows"][0]["values"]["value"] = 2
            edit["rows"][0]["geometry"] = "SRID=4326;POINT ZM(9 10 11 12)"
        def change_default():
            conn = connect()
            try:
                return database.apply_group(conn, original, changed)
            finally:
                conn.close()
        try:
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                copying = pool.submit(self.populate, reservation, worker)
                fixture.CLUSTER.wait_activity("db03-between-layers", "advisory")
                writing = pool.submit(change_default)
                try:
                    self.assertIsNotNone(writing.result(timeout=3))
                    self.assertFalse(copying.done())
                finally:
                    guard.rollback()
                branch = copying.result(timeout=10)
            self.assertEqual(self.query("SELECT source_head::text FROM managed.snapshot WHERE snapshot_id=%s",
                                        (branch["base_snapshot_id"],)), [(original["current"]["head_revision"],)])
            for d, row, mapping in zip((parent, child), (p, c), identities):
                t = database.table(d["dataset_id"])
                self.assertEqual(self.query('SELECT value,public.ST_AsEWKT(geom) FROM managed."' + t +
                                            '" WHERE version_id=%s', (branch["branch_id"],)),
                                 [(1, row["geometry"])])
                self.assertEqual(self.query('SELECT value FROM managed."' + t + '" WHERE version_id=%s',
                                            (d["default_version_id"],)), [(2,)])
                self.assertEqual(self.mapping(d), mapping)
                stored_hash = self.query("SELECT rows_sha256 FROM managed.snapshot_dataset WHERE snapshot_id=%s AND dataset_id=%s",
                                         (branch["base_snapshot_id"], d["dataset_id"]))[0][0]
                actual_hash = self.query("SELECT rows_sha256 FROM managed.snapshot_measure(%s,%s,true,%s)",
                                        (d["dataset_id"], branch["base_snapshot_id"], d["schema_sha256"]))[0][0]
                self.assertEqual(stored_hash, actual_hash)
            with self.assertRaises(psycopg2.Error):
                self.query('UPDATE managed."s_' + uuid.UUID(child["dataset_id"]).hex + '" SET parent=%s',
                           (str(uuid.uuid4()),))
        finally:
            guard.close()
            worker.close()

    def test_default_writer_already_running_does_not_advance_captured_head(self):
        d = self.dataset()
        initial = self.row()
        database.apply_rows(self.connection, d, [initial])
        d = self.refresh(d)
        before = self.stored(d, d["default_version_id"])
        self.configure(d)
        reservation = self.reserve(d)
        writer = connect()
        worker = connect()
        try:
            changed = copy.deepcopy(initial)
            changed["values"]["name"] = "later"
            with writer.cursor() as cursor:
                cursor.execute("SELECT managed.apply_rows(%s,%s,%s,%s::jsonb,false)",
                               (d["dataset_id"], d["default_version_id"], d["current"]["head_revision"], json.dumps([changed])))
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(self.populate, reservation, worker)
                try:
                    branch = future.result(timeout=3)
                finally:
                    writer.commit()
            self.assertEqual(self.stored(d, branch["branch_id"]), before)
            self.assertEqual(self.query("SELECT source_head::text FROM managed.snapshot WHERE snapshot_id=%s",
                                        (branch["base_snapshot_id"],)), [(d["current"]["head_revision"],)])
            self.assertEqual(self.query('SELECT name FROM managed."' + database.table(d["dataset_id"]) +
                                        '" WHERE version_id=%s', (d["default_version_id"],)), [("later",)])
        finally:
            writer.close()
            worker.close()

    def test_repeatable_read_reservations_and_quota_changes_cannot_overbook(self):
        import threading
        import test_managed_database as fixture
        for action in ("reserve", "configure"):
            with self.subTest(action=action):
                d = self.dataset()
                self.configure(d, max_branches=1 if action == "reserve" else 2)
                if action == "configure":
                    self.reserve(d)
                first = connect()
                second = connect()
                ready = threading.Event()
                request = (d["version_group_id"], str(uuid.uuid4()), "reserved_" + uuid.uuid4().hex,
                           str(uuid.uuid4()), "private", [], str(uuid.uuid4()))
                with first.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    cursor.execute("SELECT managed.branch_reserve(%s,%s,%s,%s,%s,%s::uuid[],%s)", request)
                def contender():
                    with second, second.cursor() as cursor:
                        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                        cursor.execute("SET application_name='db03-quota-race'")
                        cursor.execute("SELECT count(*) FROM managed.branch WHERE group_id=%s", (d["version_group_id"],))
                        cursor.fetchone()
                        ready.set()
                        if action == "reserve":
                            cursor.execute("SELECT managed.branch_reserve(%s,%s,%s,%s,%s,%s::uuid[],%s)",
                                           (d["version_group_id"], str(uuid.uuid4()), "other_" + uuid.uuid4().hex,
                                            str(uuid.uuid4()), "private", [], str(uuid.uuid4())))
                        else:
                            cursor.execute("SELECT managed.configure_branch_quotas(%s,%s::jsonb)",
                                           (d["version_group_id"], json.dumps(dict(LIMITS, max_branches=1))))
                try:
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(contender)
                        self.assertTrue(ready.wait(2))
                        fixture.CLUSTER.wait_activity("db03-quota-race", "transactionid")
                        first.commit()
                        with self.assertRaises(psycopg2.Error):
                            future.result(timeout=10)
                    count = self.query("SELECT count(*) FROM managed.branch WHERE group_id=%s AND state='creating'",
                                       (d["version_group_id"],))[0][0]
                    capacity = self.query("SELECT (limits->>'max_branches')::integer FROM managed.branch_quota WHERE group_id=%s",
                                          (d["version_group_id"],))[0][0]
                    self.assertLessEqual(count, capacity)
                finally:
                    first.close()
                    second.close()

def load_tests(loader, tests, pattern):
    return unittest.TestSuite(BranchSnapshotDatabaseTests(name)
                             for name in BranchSnapshotDatabaseTests.__dict__ if name.startswith("test_"))
