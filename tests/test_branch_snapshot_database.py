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
                         "nullable": False, "allow_empty": False, "require_valid": True},
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
                self.assertEqual(self.query("SELECT value,public.ST_AsEWKB(geom,'XDR')=public.ST_AsEWKB(public.ST_GeomFromEWKT(%s),'XDR') FROM managed.\"" + t +
                                            '\" WHERE version_id=%s', (row["geometry"], branch["branch_id"])),
                                 [(1, True)])
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
                        with self.assertRaises(psycopg2.errors.SerializationFailure):
                            future.result(timeout=10)
                    count = self.query("SELECT count(*) FROM managed.branch WHERE group_id=%s AND state='creating'",
                                       (d["version_group_id"],))[0][0]
                    capacity = self.query("SELECT (limits->>'max_branches')::integer FROM managed.branch_quota WHERE group_id=%s",
                                          (d["version_group_id"],))[0][0]
                    self.assertLessEqual(count, capacity)
                finally:
                    first.close()
                    second.close()

    def test_populated_v2_backup_restore_migration_and_user_snapshot_id_hash(self):
        import tempfile
        from pathlib import Path
        from managed.migrate import migrate, MIGRATIONS
        import test_managed_database as fixture
        definition = {"schema_version": 1, "geometry": None, "fields": [
            {"name": "snapshot_id", "type": "int32", "nullable": False},
            {"name": "branch_id", "type": "string", "nullable": False, "max_length": 20}]}
        with self.empty_database() as conn, tempfile.TemporaryDirectory() as directory:
            previous = Path(directory)
            for name in ("001_managed.sql", "002_schema_rules.sql"):
                (previous / name).write_bytes((MIGRATIONS / name).read_bytes())
            migrate(conn, previous)
            d = database.create_dataset(conn, managed_name="legacy_snapshot_field",
                catalog_item_id=str(uuid.uuid4()), policy_ref=str(uuid.uuid4()), definition=definition)
            row = {"fid": str(uuid.uuid4()), "values": {"snapshot_id": 7, "branch_id": "user attribute"}}
            database.apply_rows(conn, d, [row])
            before = database.export_schema(conn, d["dataset_id"])
            dump = fixture.CLUSTER.evidence / ("db02-branch-upgrade-" + uuid.uuid4().hex + ".sql")
            fixture.CLUSTER.tool("pg_dump", "-d", conn.info.dbname, "-f", dump)
            restored_name = "snapshot_restore_" + uuid.uuid4().hex
            fixture.CLUSTER.tool("createdb", restored_name)
            fixture.CLUSTER.tool("psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", restored_name, "-f", dump)
            restored = connect(restored_name)
            try:
                for target in (conn, restored):
                    migrate(target)
                    after = database.export_schema(target, d["dataset_id"])
                    expected = copy.deepcopy(before)
                    expected["group_schema_generation"] += 1
                    self.assertEqual(after, expected)
                    with target, target.cursor() as cursor:
                        cursor.execute("SELECT snapshot_id,branch_id FROM managed.\"" + database.table(d["dataset_id"]) + "\"")
                        self.assertEqual(cursor.fetchall(), [(7, "user attribute")])
                        cursor.execute("SELECT managed.configure_branch_quotas(%s,%s::jsonb)",
                                       (d["version_group_id"], json.dumps(LIMITS)))
                    from managed.branches import create_branch
                    branch = create_branch(target, group_id=d["version_group_id"], branch_id=str(uuid.uuid4()),
                        operation_id=str(uuid.uuid4()), name="restored_branch", owner_id=str(uuid.uuid4()))
                    with target, target.cursor() as cursor:
                        cursor.execute("SELECT snapshot_id,branch_id FROM managed.\"s_" +
                                       uuid.UUID(d["dataset_id"]).hex + "\" WHERE _snapshot_id=%s", (branch["base_snapshot_id"],))
                        self.assertEqual(cursor.fetchall(), [(7, "user attribute")])
                        cursor.execute("SELECT rows_sha256 FROM managed.snapshot_measure(%s,%s,false,%s)",
                                       (d["dataset_id"], d["default_version_id"], d["schema_sha256"]))
                        original_hash = cursor.fetchone()[0]
                        # Owned fixture changes only this legitimate user attribute.
                        # Feature revision/identity remain fixed, so they cannot explain the hash change.
                        cursor.execute("UPDATE managed.\"" + database.table(d["dataset_id"]) +
                                       "\" SET snapshot_id=8 WHERE version_id=%s", (d["default_version_id"],))
                        cursor.execute("SELECT rows_sha256 FROM managed.snapshot_measure(%s,%s,false,%s)",
                                       (d["dataset_id"], d["default_version_id"], d["schema_sha256"]))
                        self.assertNotEqual(cursor.fetchone()[0], original_hash)
            finally:
                restored.close()

    def pause_registry(self, relation, condition, *, event="INSERT"):
        import random
        key = random.randrange(1, 2000000000)
        name = "pause_" + uuid.uuid4().hex
        self.query('CREATE FUNCTION public."' + name + '"() RETURNS trigger LANGUAGE plpgsql AS $$'
                   ' BEGIN IF ' + condition + ' THEN PERFORM pg_advisory_xact_lock(' + str(key) + '); END IF; RETURN NEW; END $$')
        self.query('CREATE TRIGGER "' + name + '" BEFORE ' + event + ' ON managed."' + relation +
                   '" FOR EACH ROW EXECUTE FUNCTION public."' + name + '"()')
        guard = connect()
        with guard.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        return guard

    def test_waiting_schema_change_forces_40001_then_whole_worker_retry(self):
        from managed.branches import populate_branch, recover_branch
        from test_schema_rules_database import definition
        import test_managed_database as fixture
        attempts = []
        class TrackingCursor(psycopg2.extensions.cursor):
            def execute(self, query, parameters=None):
                if str(query).startswith("SET TRANSACTION"):
                    attempts.append(str(query))
                return super().execute(query, parameters)
        for automatic_retry in (False, True):
            with self.subTest(automatic_retry=automatic_retry):
                value = definition()
                d = self.dataset(value)
                database.apply_rows(self.connection, d, [{"fid": str(uuid.uuid4()), "values": {}}])
                d = self.refresh(d)
                self.configure(d)
                reservation = self.reserve(d)
                guard = self.pause_registry("schema_revision", "NEW.dataset_id='" + d["dataset_id"] + "'::uuid")
                writer = connect()
                worker = psycopg2.connect(host=str(fixture.CLUSTER.socket), port=fixture.CLUSTER.env["PGPORT"],
                    user=fixture.CLUSTER.env["PGUSER"], dbname="prototype", cursor_factory=TrackingCursor)
                with writer, writer.cursor() as cursor:
                    cursor.execute("SET application_name='db03-ddl-holder'")
                with worker, worker.cursor() as cursor:
                    cursor.execute("SET application_name='db03-ddl-copy'")
                changed = copy.deepcopy(value)
                changed["fields"][1]["default"] = "after_ddl"
                attempts.clear()
                try:
                    with concurrent.futures.ThreadPoolExecutor(2) as pool:
                        ddl = pool.submit(database.revise_schema, writer, d, changed)
                        fixture.CLUSTER.wait_activity("db03-ddl-holder", "advisory")
                        copying = pool.submit(populate_branch, worker, reservation) if automatic_retry else pool.submit(self.populate, reservation, worker)
                        fixture.CLUSTER.wait_activity("db03-ddl-copy", "transactionid")
                        guard.rollback()
                        revised = ddl.result(timeout=10)
                        if automatic_retry:
                            branch = copying.result(timeout=10)
                            self.assertEqual(len(attempts), 2)
                            self.assertEqual(self.query("SELECT schema_generation,source_head::text FROM managed.snapshot WHERE snapshot_id=%s",
                                (branch["base_snapshot_id"],)), [(revised["group_schema_generation"], revised["current"]["head_revision"])])
                            self.assertEqual(self.query("SELECT schema_revision_id::text FROM managed.snapshot_dataset WHERE snapshot_id=%s",
                                (branch["base_snapshot_id"],)), [(revised["schema_revision_id"],)])
                        else:
                            with self.assertRaises(psycopg2.errors.SerializationFailure):
                                copying.result(timeout=10)
                            self.assertEqual(self.query("SELECT count(*) FROM managed.snapshot WHERE branch_id=%s",
                                (reservation["branch_id"],)), [(0,)])
                            self.assertEqual(self.query("SELECT state,revision::text FROM managed.branch WHERE branch_id=%s",
                                (reservation["branch_id"],)), [("creating", reservation["revision"])])
                            recover_branch(self.connection, reservation)
                finally:
                    guard.rollback()
                    guard.close()
                    writer.close()
                    worker.close()

    def exercise_worker_interruption(self, phase, *, terminate=False, timeout=False):
        from managed.branches import create_branch, get_branch, recover_branch
        import test_managed_database as fixture
        first = self.dataset()
        second = self.dataset(version_group_id=first["version_group_id"])
        for d in (first, second):
            database.apply_rows(self.connection, self.refresh(d), [self.row()])
        self.configure(first)
        request = {"group_id": first["version_group_id"], "branch_id": str(uuid.uuid4()),
            "operation_id": str(uuid.uuid4()), "name": "interrupted_" + uuid.uuid4().hex,
            "owner_id": str(uuid.uuid4())}
        ordered = sorted([first, second], key=lambda d: d["dataset_id"])
        if phase == "seal":
            guard = self.pause_registry("snapshot", "NEW.branch_id='" + request["branch_id"] + "'::uuid", event="UPDATE")
        else:
            guard = self.pause_copy(ordered[0 if phase == "first" else 1])
        worker = connect()
        with worker, worker.cursor() as cursor:
            cursor.execute("SET application_name='db03-interrupted-worker'")
        try:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(create_branch, worker, copy_timeout_ms=1000 if timeout else 30000, **request)
                fixture.CLUSTER.wait_activity("db03-interrupted-worker", "advisory")
                self.assertIsNone(get_branch(self.connection, request["branch_id"]))
                if terminate:
                    # Exact backend belongs to this fixture's worker connection.
                    self.query("SELECT pg_terminate_backend(%s)", (worker.get_backend_pid(),))
                elif not timeout:
                    worker.cancel()
                with self.assertRaises(psycopg2.Error) as raised:
                    future.result(timeout=10)
                if timeout:
                    self.assertIn("statement timeout", str(raised.exception))
            operational = get_branch(self.connection, request["branch_id"], operational=True)
            if terminate:
                self.assertEqual(operational["state"], "creating")
                operational = recover_branch(self.connection, operational)
                self.assertEqual(operational["failure_code"], "recovered")
            self.assertEqual(operational["state"], "creation_failed")
            self.assertIsNone(get_branch(self.connection, request["branch_id"]))
            self.assertEqual(self.query("SELECT count(*) FROM managed.snapshot WHERE branch_id=%s",
                                        (request["branch_id"],)), [(0,)])
            self.assertEqual(self.query("SELECT count(*) FROM managed.commit WHERE version_id=%s",
                                        (request["branch_id"],)), [(0,)])
            for d in (first, second):
                self.assertEqual(self.stored(d, request["branch_id"]), [])
            self.assertEqual(self.query("SELECT current_rows,snapshot_rows,current_bytes,snapshot_bytes,history_rows FROM managed.branch_quota WHERE group_id=%s",
                                        (first["version_group_id"],)), [(0, 0, 0, 0, 0)])
            self.dataset(version_group_id=first["version_group_id"])  # failed reservation cannot strand schema
        finally:
            guard.rollback()
            guard.close()
            worker.close()

    def test_cancel_worker_before_first_copy(self):
        self.exercise_worker_interruption("first")

    def test_cancel_worker_between_layers(self):
        self.exercise_worker_interruption("between")

    def test_cancel_worker_before_snapshot_seal(self):
        self.exercise_worker_interruption("seal")

    def test_connection_loss_rolls_back_partial_copy_and_recovers_reservation(self):
        self.exercise_worker_interruption("between", terminate=True)

    def test_worker_statement_deadline_rolls_back_and_cleans_private_reservation(self):
        self.exercise_worker_interruption("between", timeout=True)

    def test_stable_operation_replay_name_cleanup_and_retained_lifecycle(self):
        from managed import branches
        d = self.dataset()
        database.apply_rows(self.connection, d, [self.row()])
        self.configure(d)
        request = {"group_id": d["version_group_id"], "branch_id": str(uuid.uuid4()),
                   "operation_id": str(uuid.uuid4()), "name": "durable_name", "owner_id": str(uuid.uuid4())}
        first = branches.create_branch(self.connection, **request)
        database.apply_rows(self.connection, self.refresh(d), [], replace=True)
        # Retry after a lost successful response resolves the same committed identity.
        replay = branches.create_branch(self.connection, **request)
        self.assertEqual((replay["branch_id"], replay["head_revision"], replay["base_snapshot_id"]),
                         (first["branch_id"], first["head_revision"], first["base_snapshot_id"]))
        self.assertEqual(self.query("SELECT count(*) FROM managed.snapshot WHERE branch_id=%s", (first["branch_id"],)), [(1,)])
        self.assertEqual(self.query("SELECT count(*) FROM managed.commit WHERE version_id=%s", (first["branch_id"],)), [(1,)])
        with self.assertRaisesRegex(psycopg2.Error, "BRANCH_OPERATION_CONFLICT"):
            branches.reserve_branch(self.connection, **dict(request, name="changed_request"))
        for illegal in ("_deleted_" + uuid.UUID(first["branch_id"]).hex, "_private"):
            with self.assertRaisesRegex(psycopg2.Error, "INVALID_BRANCH_REQUEST"):
                branches.update_branch(self.connection, first, name=illegal, visibility="private")
        with self.assertRaisesRegex(psycopg2.Error, "INVALID_ACTIVE_BRANCH_IDENTITY"):
            self.query("UPDATE managed.version SET name=%s WHERE version_id=%s",
                       ("_deleted_" + uuid.UUID(first["branch_id"]).hex, first["branch_id"]))
        usage = self.query("SELECT current_rows,snapshot_rows,current_bytes,snapshot_bytes,history_rows FROM managed.branch_quota WHERE group_id=%s",
                           (d["version_group_id"],))
        reference = str(uuid.uuid4())
        self.query("INSERT INTO managed.snapshot_reference VALUES(%s,'publication',%s)", (first["base_snapshot_id"], reference))
        with self.assertRaisesRegex(psycopg2.Error, "RETAINED_SNAPSHOT_REFERENCE"):
            branches.transition_branch(self.connection, first, "deletion_pending")
        archived = branches.transition_branch(self.connection, first, "archived")
        with self.assertRaisesRegex(psycopg2.Error, "STALE_BRANCH_METADATA"):
            branches.transition_branch(self.connection, first, "deletion_pending")
        self.query("DELETE FROM managed.snapshot_reference WHERE snapshot_id=%s AND kind='publication' AND reference_id=%s",
                   (first["base_snapshot_id"], reference))
        pending = branches.transition_branch(self.connection, archived, "deletion_pending")
        deleted = branches.transition_branch(self.connection, pending, "deleted")
        self.assertEqual(deleted["name"], "durable_name")
        self.assertIsNone(branches.get_branch(self.connection, first["branch_id"]))
        self.assertEqual(self.query("SELECT name FROM managed.version WHERE version_id=%s", (first["branch_id"],)),
                         [("_deleted_" + uuid.UUID(first["branch_id"]).hex,)])
        self.assertEqual(usage, self.query("SELECT current_rows,snapshot_rows,current_bytes,snapshot_bytes,history_rows FROM managed.branch_quota WHERE group_id=%s",
                                          (d["version_group_id"],)))
        with self.assertRaisesRegex(ValueError, "ACTIVE_BRANCH_SCHEMA_CHANGE_UNSUPPORTED"):
            self.dataset(version_group_id=d["version_group_id"])
        new_request = dict(request, branch_id=str(uuid.uuid4()), operation_id=str(uuid.uuid4()))
        replacement = branches.reserve_branch(self.connection, **new_request)
        self.assertNotEqual(replacement["branch_id"], deleted["branch_id"])
        old_replay = branches.create_branch(self.connection, **request)
        self.assertEqual((old_replay["branch_id"], old_replay["state"]), (deleted["branch_id"], "deleted"))
        failed = branches.cancel_branch(self.connection, replacement)
        terminal = branches.transition_branch(self.connection, failed, "deleted")
        self.assertEqual(terminal["state"], "deleted")
        self.assertEqual(self.query("SELECT count(*) FROM managed.snapshot WHERE branch_id=%s", (terminal["branch_id"],)), [(0,)])
        newest = branches.reserve_branch(self.connection, **dict(request, branch_id=str(uuid.uuid4()), operation_id=str(uuid.uuid4())))
        self.assertEqual(newest["name"], "durable_name")

    def test_branch_worker_has_only_finite_routines_and_no_raw_table_access(self):
        from managed import branches
        d = self.dataset()
        database.apply_rows(self.connection, d, [self.row()])
        self.configure(d)
        role = "branch_worker_" + uuid.uuid4().hex
        self.query('CREATE ROLE "' + role + '"')
        database.grant_branch_worker(self.connection, role)
        database.grant_branch_worker(self.connection, role)  # grant is safely repeatable
        conn = connect()
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute('SET ROLE "' + role + '"')
                cursor.execute("CREATE TEMP TABLE branch(branch_id uuid); SET search_path=pg_temp,public")
            branch = branches.create_branch(conn, group_id=d["version_group_id"], branch_id=str(uuid.uuid4()),
                operation_id=str(uuid.uuid4()), name="worker_branch", owner_id=str(uuid.uuid4()))
            self.assertEqual(branch["state"], "active")
            self.assertEqual([b["branch_id"] for b in branches.list_branches(conn, d["version_group_id"])], [branch["branch_id"]])
            for statement in [
                "SELECT * FROM managed.branch",
                "UPDATE managed.branch SET state='deleted'",
                "SELECT managed.configure_branch_quotas(NULL,NULL)",
                "SELECT managed.install_snapshot_table(NULL)",
                "SELECT managed.snapshot_measure(NULL,NULL,false,NULL)",
                "SELECT managed.raw_apply_rows(NULL,NULL,NULL,NULL,false)",
                'DELETE FROM managed."' + database.table(d["dataset_id"]) + '"',
                'TRUNCATE managed."s_' + uuid.UUID(d["dataset_id"]).hex + '"',
                "DELETE FROM managed.snapshot_reference",
                "DELETE FROM managed.commit",
            ]:
                with self.subTest(statement=statement), self.assertRaises(psycopg2.Error):
                    with conn, conn.cursor() as cursor:
                        cursor.execute(statement)
            with self.assertRaises(psycopg2.Error):
                database.apply_rows(conn, self.refresh(d), [self.row()])
            conn.rollback()
        finally:
            conn.close()
        denied = connect()
        try:
            with denied, denied.cursor() as cursor:
                cursor.execute("SET ROLE outsider")
            with self.assertRaises(psycopg2.Error):
                branches.list_branches(denied, d["version_group_id"])
        finally:
            denied.close()

    def test_concurrent_copy_quota_accounts_retained_typed_rows_and_commit(self):
        from managed.branches import populate_branch, recover_branch, transition_branch
        import test_managed_database as fixture
        d = self.dataset()
        database.apply_rows(self.connection, d, [self.row()])
        self.configure(d, max_current_rows=1, max_snapshot_rows=1, max_history_rows=1)
        first = self.reserve(d)
        second = self.reserve(d)
        guard = self.pause_copy(d)
        a, b = connect(), connect()
        with a, a.cursor() as cursor:
            cursor.execute("SET application_name='db03-quota-copy-first'")
        with b, b.cursor() as cursor:
            cursor.execute("SET application_name='db03-quota-copy-second'")
        try:
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                one = pool.submit(populate_branch, a, first)
                fixture.CLUSTER.wait_activity("db03-quota-copy-first", "advisory")
                two = pool.submit(populate_branch, b, second)
                fixture.CLUSTER.wait_activity("db03-quota-copy-second", "transactionid")
                guard.rollback()
                active = one.result(timeout=10)
                with self.assertRaisesRegex(psycopg2.Error, "BRANCH_STORAGE_QUOTA"):
                    two.result(timeout=10)
            recover_branch(self.connection, second)
            actual = self.query("SELECT current_rows,snapshot_rows,history_rows,attachment_bytes FROM managed.branch_quota WHERE group_id=%s",
                                (d["version_group_id"],))
            self.assertEqual(actual, [(1, 1, 1, 0)])
            for snapshot, identity_value, field in [(False, active["branch_id"], "current_bytes"),
                                                    (True, active["base_snapshot_id"], "snapshot_bytes")]:
                measured = self.query("SELECT row_bytes FROM managed.snapshot_measure(%s,%s,%s,%s)",
                                     (d["dataset_id"], identity_value, snapshot, d["schema_sha256"]))[0][0]
                self.assertEqual(self.query("SELECT " + field + " FROM managed.branch_quota WHERE group_id=%s",
                                            (d["version_group_id"],)), [(measured,)])
            pending = transition_branch(self.connection, active, "deletion_pending")
            transition_branch(self.connection, pending, "deleted")
            self.assertEqual(actual, self.query("SELECT current_rows,snapshot_rows,history_rows,attachment_bytes FROM managed.branch_quota WHERE group_id=%s",
                                               (d["version_group_id"],)))
        finally:
            guard.rollback()
            guard.close()
            a.close()
            b.close()

    def test_source_bounds_and_expired_empty_reservations_fail_without_partial_rows(self):
        from managed import branches
        value = {"schema_version": 1, "geometry": None,
                 "fields": [{"name": "text_value", "type": "string", "nullable": False, "max_length": 100000}]}
        d = self.dataset(value)
        database.apply_rows(self.connection, d, [{"fid": str(uuid.uuid4()), "values": {"text_value": "a" * 100000}}])
        self.configure(d, max_source_bytes=2048)
        request = {"group_id": d["version_group_id"], "branch_id": str(uuid.uuid4()),
                   "operation_id": str(uuid.uuid4()), "name": "bounded", "owner_id": str(uuid.uuid4())}
        with self.assertRaisesRegex(psycopg2.Error, "BRANCH_SOURCE_BOUND"):
            branches.create_branch(self.connection, **request)
        failed = branches.get_branch(self.connection, request["branch_id"], operational=True)
        self.assertEqual(failed["state"], "creation_failed")
        self.assertEqual(self.query("SELECT count(*) FROM managed.snapshot WHERE branch_id=%s", (request["branch_id"],)), [(0,)])
        self.configure(d)
        stale = branches.reserve_branch(self.connection, **dict(request, branch_id=str(uuid.uuid4()),
            operation_id=str(uuid.uuid4()), name="expired"))
        self.query("UPDATE managed.branch SET created_at=created_at-interval '2 days' WHERE branch_id=%s", (stale["branch_id"],))
        with self.assertRaisesRegex(psycopg2.Error, "BRANCH_IDLE_EXPIRED"):
            branches.populate_branch(self.connection, stale)
        branches.recover_branch(self.connection, stale)

    def test_exact_canonical_byte_bounds_include_geometry_text_and_deleted_retention(self):
        from decimal import Decimal
        from managed import branches
        from test_schema import DEFINITION
        definition = copy.deepcopy(DEFINITION)
        definition["geometry"]["dimensions"] = "XYZM"
        d = self.dataset(definition)
        row = self.row(values={"name": 'é"水', "amount": "1.125"},
                       geometry="SRID=4326;POINT ZM(1.25 2.5 3.75 4)")
        database.apply_rows(self.connection, d, [row])
        current = database.table(d["dataset_id"])
        snapshot = "s_" + uuid.UUID(d["dataset_id"]).hex
        # Preserve every generated typed/domain/rule CHECK in the sealed table.
        check_sql = "SELECT pg_get_constraintdef(oid,true) FROM pg_constraint WHERE conrelid=%s::regclass AND contype='c'"
        source_checks = set(self.query(check_sql, ("managed." + current,)))
        self.assertTrue(source_checks)
        self.assertEqual(source_checks, set(self.query(check_sql, ("managed." + snapshot,))))
        # Independently construct the fixed scalar + XDR payload. JSONB key order
        # does not affect byte length; JSON spacing and numeric scale do.
        columns = ["dataset_id", "version_group_id", "fid", "object_id", "feature_revision", "name", "amount", "geom"]
        values = self.query('SELECT dataset_id::text,version_group_id::text,fid::text,object_id,'
                            "feature_revision::text,name,amount,encode(public.ST_AsEWKB(geom,'XDR'),'hex')"
                            ' FROM managed."' + current + '" WHERE version_id=%s', (d["default_version_id"],))[0]
        encode = lambda value: str(value) if isinstance(value, Decimal) else json.dumps(value, ensure_ascii=False)
        payload = "{" + ", ".join(json.dumps(k) + ": " + encode(v) for k, v in zip(columns, values)) + "}"
        exact = len(payload.encode("utf-8"))
        measured = self.query("SELECT row_bytes FROM managed.snapshot_measure(%s,%s,false,%s)",
                              (d["dataset_id"], d["default_version_id"], d["schema_sha256"]))[0][0]
        self.assertEqual(exact, measured)
        self.configure(d, max_source_rows=1, max_source_bytes=exact, max_current_bytes=exact, max_snapshot_bytes=exact)
        request = {"group_id": d["version_group_id"], "branch_id": str(uuid.uuid4()),
                   "operation_id": str(uuid.uuid4()), "name": "exact_bound", "owner_id": str(uuid.uuid4())}
        branch = branches.create_branch(self.connection, **request)
        self.assertEqual(self.query("SELECT row_bytes FROM managed.snapshot_dataset WHERE snapshot_id=%s",
                                    (branch["base_snapshot_id"],)), [(exact,)])
        pending = branches.transition_branch(self.connection, branch, "deletion_pending")
        branches.transition_branch(self.connection, pending, "deleted")
        self.assertEqual(self.query("SELECT current_bytes,snapshot_bytes FROM managed.branch_quota WHERE group_id=%s",
                                    (d["version_group_id"],)), [(exact, exact)])
        # Release the public name, but retained payloads still consume both quotas.
        self.configure(d, max_source_rows=1, max_source_bytes=exact,
                       max_current_bytes=exact * 3, max_snapshot_bytes=exact * 3)
        row["values"]["name"] += "x"
        database.apply_rows(self.connection, self.refresh(d), [row])
        self.assertEqual(self.query("SELECT row_bytes FROM managed.snapshot_measure(%s,%s,false,%s)",
                                    (d["dataset_id"], d["default_version_id"], d["schema_sha256"])), [(exact + 1,)])
        request.update(branch_id=str(uuid.uuid4()), operation_id=str(uuid.uuid4()))
        with self.assertRaisesRegex(psycopg2.Error, "BRANCH_SOURCE_BOUND"):
            branches.create_branch(self.connection, **request)
        self.assertEqual(self.query("SELECT current_bytes,snapshot_bytes FROM managed.branch_quota WHERE group_id=%s",
                                    (d["version_group_id"],)), [(exact, exact)])

def load_tests(loader, tests, pattern):
    return unittest.TestSuite(BranchSnapshotDatabaseTests(name)
                             for name in BranchSnapshotDatabaseTests.__dict__ if name.startswith("test_"))
