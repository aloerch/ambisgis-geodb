# SPDX-License-Identifier: GPL-3.0-or-later
import json
import random
import unittest
from uuid import uuid4

from prototype.database import DEFAULT, literal
from prototype.reference import reconcile

CLUSTER = None
BASELINE = False
FID = '10000000-0000-0000-0000-000000000001'
GEO = 'SRID=4326;POINT Z(1 2 3)'


class DatabaseAcceptance(unittest.TestCase):
    def setUp(self):
        self.db = CLUSTER
        if self.db is None:
            self.fail('Real owned database required: run python3 tools/run_acceptance.py --prefix ...')
        self.db.reset(BASELINE)
        for dataset in ('assets', 'observations'):
            self.db.sql(f"SELECT edit('{DEFAULT}',{self.head(DEFAULT)},'{dataset}','insert','{FID}','seed',1,'{GEO}');")

    def head(self, v):
        return int(self.db.sql(f"SELECT head FROM versions WHERE id='{v}';"))

    def branch(self, name=None):
        return self.db.transaction(f'SELECT create_branch({literal(name or str(uuid4()))});')

    def edit(self, v, dataset='assets', name='seed', value=2, geom=GEO, op='update', fid=FID):
        return self.db.sql(f'SELECT edit({literal(v)},{self.head(v)},{literal(dataset)},{literal(op)},'
                           f'{literal(fid)},{literal(name)},{value},{literal(geom)});')

    def state(self, v, dataset='assets'):
        return json.loads(self.db.sql(f"SELECT coalesce(jsonb_object_agg(feature_id,jsonb_build_object("
                          f"'name',name,'value',value,'geom',encode(ST_AsEWKB(geom,'NDR'),'hex'))),'{{}}')"
                          f" FROM current_{dataset} WHERE version_id='{v}';"))

    def prepare(self, v):
        return self.db.transaction(f"SELECT prepare_reconcile('{v}');")

    def accept(self, p):
        return int(self.db.sql(f"SELECT accept_reconcile('{p}');"))

    def post_sql(self, p, request=None):
        r = json.loads(self.db.sql(f"SELECT jsonb_build_array(accepted_head,target_head) FROM plans WHERE id='{p}';"))
        return f"SELECT post('{p}',{r[0]},{r[1]},'{request or uuid4()}');"

    def candidate(self, p):
        return json.loads(self.db.sql(f"SELECT coalesce(jsonb_object_agg(feature_id,jsonb_build_object("
                          "'name',name,'value',value,'geom',encode(ST_AsEWKB(geom,'NDR'),'hex'))),'{}')"
                          f" FROM snapshot_assets WHERE snapshot_id=(SELECT candidate FROM plans WHERE id='{p}');"))

    def test_01_base_isolation_across_datasets(self):
        branch = self.branch()
        captured = [self.state(branch, d) for d in ('assets', 'observations')]
        self.edit(DEFAULT, value=9)
        self.edit(DEFAULT, dataset='observations', value=8)
        self.assertEqual(captured, [self.state(branch, d) for d in ('assets', 'observations')])

    def test_02_field_merge_matches_reference_and_accept_is_private(self):
        base = self.state(DEFAULT)
        branch = self.branch()
        self.edit(branch, name='branch', value=1)
        self.edit(DEFAULT, value=7)
        expected, conflicts = reconcile(base, self.state(branch), self.state(DEFAULT))
        self.assertFalse(conflicts)
        target = self.state(DEFAULT)
        plan = self.prepare(branch)
        self.assertEqual(expected, self.candidate(plan))
        self.accept(plan)
        self.assertEqual(expected, self.state(branch))
        self.assertEqual(target, self.state(DEFAULT))

    def test_03_same_field_conflict_requires_explicit_resolution(self):
        branch = self.branch()
        self.edit(branch, value=2)
        self.edit(DEFAULT, value=3)
        plan = self.prepare(branch)
        self.assertEqual('{value}', self.db.sql(f"SELECT fields FROM conflicts WHERE plan_id='{plan}';"))
        self.db.sql(f"SELECT accept_reconcile('{plan}');", error='UNRESOLVED_CONFLICT')
        self.db.sql(f"SELECT resolve_conflict('{plan}','assets','{FID}','ours');")
        self.accept(plan)
        self.db.sql(self.post_sql(plan))
        self.assertEqual(2, self.state(DEFAULT)[FID]['value'])

    def test_04_target_changed_during_review_rejects_accept(self):
        branch = self.branch()
        plan = self.prepare(branch)
        self.edit(DEFAULT)
        self.db.sql(f"SELECT accept_reconcile('{plan}');", error='STALE_RECONCILE')

    def test_05_target_changed_after_accept_rejects_post(self):
        branch = self.branch()
        self.edit(branch)
        plan = self.prepare(branch)
        self.accept(plan)
        self.edit(DEFAULT, dataset='observations')
        target = self.state(DEFAULT)
        self.db.sql(self.post_sql(plan), error='STALE_RECONCILE')
        self.assertEqual(target, self.state(DEFAULT))

    def test_06_branch_edit_after_accept_rejects_post(self):
        branch = self.branch()
        plan = self.prepare(branch)
        self.accept(plan)
        self.edit(branch)
        self.db.sql(self.post_sql(plan), error='STALE_RECONCILE')

    def test_07_atomic_group_post_and_idempotent_retry(self):
        branch = self.branch()
        self.edit(branch, value=11)
        self.edit(branch, dataset='observations', value=12)
        plan = self.prepare(branch)
        self.accept(plan)
        statement = self.post_sql(plan)
        result = self.db.sql(statement)
        self.assertEqual(result, self.db.sql(statement))
        self.assertEqual(11, self.state(DEFAULT)[FID]['value'])
        self.assertEqual(12, self.state(DEFAULT,'observations')[FID]['value'])
        self.assertEqual('1', self.db.sql("SELECT count(*) FROM events WHERE operation='post';"))
        self.assertEqual('0', self.db.sql('SELECT count(*) FROM events e LEFT JOIN outbox o ON e.id=o.event_id WHERE o.event_id IS NULL;'))

    def test_08_post_backend_crash_rolls_back_heads_rows_audit_request(self):
        branch = self.branch()
        self.edit(branch, value=11)
        self.edit(branch, dataset='observations', value=12)
        plan = self.prepare(branch)
        self.accept(plan)
        state = [self.state(DEFAULT,d) for d in ('assets','observations')]
        heads = (self.head(DEFAULT),self.head(branch))
        count = self.db.sql('SELECT count(*) FROM events;')
        statement = self.post_sql(plan)
        backend = self.db.background('BEGIN;'+statement+'SELECT pg_sleep(25); COMMIT;', 'fnd04_crash')
        try:
            pid = self.db.wait_activity('fnd04_crash','PgSleep')
            self.assertEqual(state,[self.state(DEFAULT,d) for d in ('assets','observations')])
            self.db.sql(f'SELECT pg_terminate_backend({pid});')
            _, stderr = backend.communicate(timeout=10)
            self.assertNotEqual(0,backend.returncode,stderr)
        finally:
            if backend.poll() is None:
                backend.terminate()
                backend.communicate(timeout=10)
        self.assertEqual(heads,(self.head(DEFAULT),self.head(branch)))
        self.assertEqual(state,[self.state(DEFAULT,d) for d in ('assets','observations')])
        self.assertEqual(count,self.db.sql('SELECT count(*) FROM events;'))
        self.assertEqual('0',self.db.sql('SELECT count(*) FROM requests;'))
        self.db.sql(statement)
        self.assertEqual(12,self.state(DEFAULT,'observations')[FID]['value'])

    def test_09_two_concurrent_posts_only_one_wins(self):
        a,b = self.branch(),self.branch()
        self.edit(a,value=4)
        self.edit(b,value=5)
        pa,pb = self.prepare(a),self.prepare(b)
        self.accept(pa)
        self.accept(pb)
        first = self.db.background('BEGIN;'+self.post_sql(pa)+'SELECT pg_sleep(2);COMMIT;', 'fnd04_winner')
        self.db.wait_activity('fnd04_winner','PgSleep')
        second = self.db.background(self.post_sql(pb),'fnd04_loser')
        try:
            self.db.wait_activity('fnd04_loser','transactionid')
            _,e1 = first.communicate(timeout=10)
            _,e2 = second.communicate(timeout=10)
            self.assertEqual(0,first.returncode,e1)
            self.assertNotEqual(0,second.returncode)
            self.assertIn('STALE_RECONCILE',e2)
        finally:
            for process in (first,second):
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=10)
        self.assertEqual(4,self.state(DEFAULT)[FID]['value'])
        self.assertEqual('1',self.db.sql("SELECT count(*) FROM events WHERE operation='post';"))

    def test_10_geometry_z_is_atomic(self):
        branch = self.branch()
        self.edit(branch,geom='SRID=4326;POINT Z(1 2 4)',value=1)
        self.edit(DEFAULT,geom='SRID=4326;POINT Z(1 2 5)',value=1)
        plan = self.prepare(branch)
        self.assertEqual('{geom}',self.db.sql(f"SELECT fields FROM conflicts WHERE plan_id='{plan}';"))

    def test_11_delete_update_conflict(self):
        branch = self.branch()
        self.edit(branch,op='delete')
        self.edit(DEFAULT)
        plan = self.prepare(branch)
        self.assertEqual('{existence}',self.db.sql(f"SELECT fields FROM conflicts WHERE plan_id='{plan}';"))

    def test_12_duplicate_identity_insert_conflict(self):
        branch = self.branch()
        fid = str(uuid4())
        self.edit(branch,op='insert',fid=fid,name='new',value=3)
        self.edit(DEFAULT,op='insert',fid=fid,name='new',value=4)
        plan = self.prepare(branch)
        self.assertEqual('{existence}',self.db.sql(f"SELECT fields FROM conflicts WHERE plan_id='{plan}';"))

    def test_13_combined_natural_key_collision_rolls_back_plan(self):
        branch = self.branch()
        self.edit(branch,op='insert',fid=str(uuid4()),name='collision')
        self.edit(DEFAULT,op='insert',fid=str(uuid4()),name='collision')
        self.db.sql(f"BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT prepare_reconcile('{branch}'); COMMIT;",error='duplicate key')
        self.assertEqual('0',self.db.sql('SELECT count(*) FROM plans;'))

    def test_14_stale_head_and_typed_constraints(self):
        branch = self.branch()
        self.db.sql(f"SELECT edit('{branch}',1,'assets','update','{FID}','seed',4,'{GEO}');",error='STALE_BRANCH_HEAD')
        self.db.sql(f"SELECT edit('{branch}',NULL,'assets','update','{FID}','seed',4,'{GEO}');",error='STALE_BRANCH_HEAD')
        for value,geom,error in [(-1,GEO,'check constraint'),(1,'SRID=3857;POINT Z(1 2 3)','SRID'),(1,'SRID=4326;POINT(1 2)','Z dimension')]:
            self.db.sql(f"SELECT edit('{branch}',0,'assets','update','{FID}','seed',{value},'{geom}');",error=error)
        self.assertEqual(0,self.head(branch))

    def test_15_no_public_direct_write_or_function_bypass(self):
        self.db.sql('SET ROLE outsider; UPDATE geodb.current_assets SET value=999;',error='permission denied')
        self.db.sql("SET ROLE outsider; SELECT geodb.create_branch('bypass');",error='permission denied')

    def test_16_immutable_snapshot_and_isolation_level(self):
        branch = self.branch()
        self.db.sql(f"UPDATE snapshot_assets SET value=9 WHERE snapshot_id=(SELECT base FROM versions WHERE id='{branch}');",error='IMMUTABLE_SNAPSHOT')
        self.db.sql("SELECT create_branch('unsafe');",error='REPEATABLE_READ_REQUIRED')

    def test_17_schema_change_invalidates_reconcile(self):
        branch = self.branch()
        plan = self.prepare(branch)
        self.db.sql('UPDATE version_group SET generation=2;')
        self.db.sql(f"SELECT accept_reconcile('{plan}');",error='STALE_RECONCILE')

    def test_18_request_reuse_changed_payload_rejected(self):
        branch = self.branch()
        plan = self.prepare(branch)
        self.accept(plan)
        request = str(uuid4())
        self.db.sql(self.post_sql(plan,request))
        self.db.sql(f"SELECT post('{plan}',999,999,'{request}');",error='IDEMPOTENCY_CONFLICT')

    def test_19_randomized_database_merge_against_pure_oracle(self):
        rng = random.Random(20261002)
        outcomes = {'conflict':0,'clean':0}
        for i in range(35):
            base = self.state(DEFAULT)
            branch = self.branch()
            # Exercise equal, disjoint and overlapping field edits deterministically.
            self.edit(branch,name='seed' if rng.randrange(2) else f'ours{i}',value=rng.randrange(1,5))
            self.edit(DEFAULT,name='seed' if rng.randrange(2) else f'target{i}',value=rng.randrange(1,5))
            expected,conflicts = reconcile(base,self.state(branch),self.state(DEFAULT))
            plan = self.prepare(branch)
            actual_conflicts = json.loads(self.db.sql(f"SELECT coalesce(jsonb_object_agg(feature_id,fields),'{{}}') FROM conflicts WHERE plan_id='{plan}' AND dataset='assets';"))
            self.assertEqual(conflicts,actual_conflicts)
            self.assertEqual(expected,self.candidate(plan))
            outcomes['conflict' if conflicts else 'clean'] += 1
            if not conflicts:
                self.accept(plan)
                self.db.sql(self.post_sql(plan))
                self.assertEqual(expected,self.state(DEFAULT))
        self.assertGreater(outcomes['clean'],0)
        self.assertGreater(outcomes['conflict'],0)

    def test_20_equal_change_and_both_delete_coalesce(self):
        branch = self.branch()
        self.edit(branch,value=5)
        self.edit(DEFAULT,value=5)
        plan = self.prepare(branch)
        self.accept(plan)
        self.db.sql(self.post_sql(plan))
        other = self.branch()
        self.edit(other,op='delete')
        self.edit(DEFAULT,op='delete')
        plan = self.prepare(other)
        self.accept(plan)
        self.db.sql(self.post_sql(plan))
        self.assertEqual({},self.state(DEFAULT))

    def test_21_creation_uses_one_snapshot_during_concurrent_target_edit(self):
        before = [self.state(DEFAULT,d) for d in ('assets','observations')]
        worker = self.db.background("BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT head FROM versions WHERE name='DEFAULT'; SELECT pg_sleep(2); SELECT create_branch('concurrent-copy'); COMMIT;",'fnd04_copy')
        try:
            self.db.wait_activity('fnd04_copy','PgSleep')
            self.edit(DEFAULT,value=20)
            self.edit(DEFAULT,dataset='observations',value=21)
            _,stderr = worker.communicate(timeout=10)
            self.assertEqual(0,worker.returncode,stderr)
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.communicate(timeout=10)
        branch = self.db.sql("SELECT id FROM versions WHERE name='concurrent-copy';")
        self.assertEqual(before,[self.state(branch,d) for d in ('assets','observations')])

    def test_22_prefiltered_post_preserves_insert_and_delete(self):
        branch = self.branch()
        new_id = str(uuid4())
        self.edit(branch,op='delete')
        self.edit(branch,dataset='observations',op='insert',fid=new_id,name='new',value=8)
        plan = self.prepare(branch)
        self.accept(plan)
        self.db.sql(self.post_sql(plan))
        self.assertEqual({},self.state(DEFAULT))
        self.assertEqual(8,self.state(DEFAULT,'observations')[new_id]['value'])
        self.assertEqual(1,self.state(DEFAULT,'observations')[FID]['value'])

    def test_23_deferred_natural_key_swap_is_atomic(self):
        other = str(uuid4())
        self.edit(DEFAULT,op='insert',fid=other,name='other')
        branch = self.branch()
        self.db.sql(f"BEGIN; SELECT edit('{branch}',0,'assets','update','{FID}','other',1,'{GEO}'); SELECT edit('{branch}',1,'assets','update','{other}','seed',2,'{GEO}'); COMMIT;")
        plan = self.prepare(branch)
        self.accept(plan)
        self.db.sql(self.post_sql(plan))
        self.assertEqual('other',self.state(DEFAULT)[FID]['name'])
        self.assertEqual('seed',self.state(DEFAULT)[other]['name'])
