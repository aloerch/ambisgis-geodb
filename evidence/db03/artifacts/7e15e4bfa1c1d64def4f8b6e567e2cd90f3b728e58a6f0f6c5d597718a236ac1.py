# SPDX-License-Identifier: GPL-3.0-or-later
"""DB-02 real database acceptance on the owned disposable cluster."""
import copy
import unittest
import uuid
import psycopg2
import managed.database as database
from test_managed_database import ManagedDatabaseTests, connect


def definition():
    return {"schema_version":2,"geometry":None,"fields":[
        {"name":"kind","type":"int32","nullable":False,"default":1},
        {"name":"label","type":"string","max_length":40,"nullable":False,"default":"plain"},
        {"name":"amount","type":"int32","nullable":False,"default":5,"domain":{"kind":"range","min":0,"max":20}},
        {"name":"limit_value","type":"int32","nullable":False,"default":10},
        {"name":"status","type":"string","max_length":20,"nullable":True,"default":"new","domain":{"kind":"coded","values":["new","old"]}}],
        "subtypes":{"field":"kind","variants":[
            {"code":1,"defaults":{},"domains":{}},
            {"code":2,"defaults":{"label":"special","amount":8},"domains":{"amount":{"kind":"range","min":6,"max":12}}}]},
        "relationships":[],"rules":[{"name":"amount_limit","kind":"compare","field":"amount","operator":"le","operand":{"field":"limit_value"}}]}


class SchemaRulesDatabaseTests(ManagedDatabaseTests):
    # Inherited DB01 cases are loaded separately, not duplicated in this suite.
    def row2(self, values=None):
        return {"fid":str(uuid.uuid4()),"values":{} if values is None else values}

    def test_defaults_domains_subtypes_and_atomic_rejection(self):
        d=self.dataset(definition());row=self.row2({"kind":2})
        database.apply_rows(self.connection,d,[row]);d=self.refresh(d)
        t=database.table(d["dataset_id"])
        self.assertEqual(self.query(f"SELECT kind,label,amount,status FROM managed.\"{t}\""),[(2,"special",8,"new")])
        for values in ({"kind":3},{"kind":2,"amount":5},{"amount":21},{"status":"other"},{"amount":11},{"amount":None}):
            self.rejects(d,[self.row2(),self.row2(values)])
        explicit=self.row2({"status":None})
        database.apply_rows(self.connection,self.refresh(d),[explicit])
        self.assertEqual(self.query(f"SELECT status FROM managed.\"{t}\" WHERE fid=%s",(explicit["fid"],)),[(None,)])
        self.rejects(d,[row])  # existing full-row update cannot silently reset defaults

    def test_relation_group_atomicity_restrict_cardinality_and_version_scope(self):
        parent=self.dataset({"schema_version":1,"fields":[{"name":"name","type":"string","max_length":20,"nullable":False}],"geometry":None})
        value=definition();value["fields"].append({"name":"parent","type":"uuid","nullable":False})
        value["relationships"]=[{"name":"owner","field":"parent","target_dataset":parent["dataset_id"],"cardinality":"one-to-one","on_delete":"restrict"}]
        child=self.dataset(value,version_group_id=parent["version_group_id"])
        p={"fid":str(uuid.uuid4()),"values":{"name":"parent"}};c=self.row2({"parent":p["fid"]})
        edits=[{"dataset_id":child["dataset_id"],"rows":[c],"replace":False},{"dataset_id":parent["dataset_id"],"rows":[p],"replace":False}]
        database.apply_group(self.connection,self.refresh(parent),edits)
        self.rejects(child,[self.row2({"parent":p["fid"]}),self.row2({"parent":p["fid"]})])
        self.rejects(parent,[])
        before=(self.state(parent),self.state(child))
        invalid=copy.deepcopy(edits);invalid[0]["rows"]=[self.row2({"parent":str(uuid.uuid4())})]
        with self.assertRaises(psycopg2.Error):database.apply_group(self.connection,self.refresh(parent),invalid)
        self.assertEqual(before,(self.state(parent),self.state(child)))
        other=self.dataset()
        with self.assertRaises(psycopg2.Error):database.apply_group(self.connection,self.refresh(parent),[{"dataset_id":other["dataset_id"],"rows":[],"replace":True}])
        database.apply_group(self.connection,self.refresh(parent),[{"dataset_id":parent["dataset_id"],"rows":[],"replace":True},{"dataset_id":child["dataset_id"],"rows":[],"replace":True}])
        self.assertEqual(len(self.mapping(parent)),1);self.assertEqual(len(self.mapping(child)),1)

    def test_retained_schema_revision_and_failed_change_preserve_identity(self):
        d=self.dataset(definition());row=self.row2({"amount":7})
        database.apply_rows(self.connection,d,[row]);d=self.refresh(d);before=self.mapping(d)
        value=definition();value["fields"][2]["domain"]["max"]=9
        changed=database.revise_schema(self.connection,d,value)
        self.assertNotEqual(changed["schema_revision_id"],d["schema_revision_id"])
        self.assertEqual(changed["group_schema_generation"],d["group_schema_generation"]+1)
        self.assertEqual(before,self.mapping(d))
        self.assertEqual(self.query("SELECT count(*) FROM managed.schema_revision WHERE dataset_id=%s",(d["dataset_id"],)),[(2,)])
        state=self.state(d);value["fields"][2]["domain"]["max"]=6
        with self.assertRaises((ValueError,psycopg2.Error)):database.revise_schema(self.connection,changed,value)
        self.assertEqual(state,self.state(d))
        with self.assertRaisesRegex(ValueError,"STALE_SCHEMA"):database.revise_schema(self.connection,d,definition())

    def test_service_cannot_execute_raw_or_schema_functions(self):
        d=self.dataset(definition());writer="rules_"+uuid.uuid4().hex
        self.query(f"CREATE ROLE \"{writer}\"");database.grant_service(self.connection,writer)
        conn=connect()
        try:
            with conn,conn.cursor() as c:c.execute(f"SET ROLE \"{writer}\"")
            database.apply_rows(conn,d,[self.row2()])
            self.assertEqual(self.query("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='managed' AND has_function_privilege(%s,p.oid,'EXECUTE') ORDER BY p.proname",(writer,)),[("apply_group",),("apply_rows",)])
        finally:conn.close()

    def test_existing_v1_backup_restore_and_forward_migration_preserve_grants(self):
        import tempfile
        from pathlib import Path
        from managed.migrate import migrate, MIGRATIONS
        import test_managed_database as fixture
        with self.empty_database() as conn, tempfile.TemporaryDirectory() as directory:
            old=Path(directory);(old/'001_managed.sql').write_bytes((MIGRATIONS/'001_managed.sql').read_bytes())
            migrate(conn,old)
            d=database.create_dataset(conn,managed_name='legacy',catalog_item_id=str(uuid.uuid4()),
                policy_ref=str(uuid.uuid4()),definition={'schema_version':1,'geometry':None,
                'fields':[{'name':'name','type':'string','max_length':40,'nullable':False}]})
            row={'fid':str(uuid.uuid4()),'values':{'name':'before migration'}}
            database.apply_rows(conn,d,[row]);before=database.export_schema(conn,d['dataset_id'])
            writer='upgrade_'+uuid.uuid4().hex
            with conn,conn.cursor() as c:
                c.execute(f'CREATE ROLE "{writer}"; GRANT USAGE ON SCHEMA managed TO "{writer}"; GRANT EXECUTE ON FUNCTION managed.apply_rows(uuid,uuid,uuid,jsonb,boolean) TO "{writer}"')
                c.execute(f'SELECT fid,object_id,feature_revision FROM managed."{database.table(d["dataset_id"])}"');identities=c.fetchall()
            dump=fixture.CLUSTER.evidence/('db01-backup-'+uuid.uuid4().hex+'.sql')
            fixture.CLUSTER.tool('pg_dump','-d',conn.info.dbname,'-f',dump)
            migrate(conn)
            upgraded=copy.deepcopy(before);upgraded['group_schema_generation']+=1
            self.assertEqual(database.export_schema(conn,d['dataset_id']),upgraded)
            with conn,conn.cursor() as c:
                c.execute("SELECT has_function_privilege(%s,'managed.apply_rows(uuid,uuid,uuid,jsonb,boolean)','EXECUTE'),has_function_privilege(%s,'managed.raw_apply_rows(uuid,uuid,uuid,jsonb,boolean)','EXECUTE')",(writer,writer))
                self.assertEqual(c.fetchone(),(True,False))
                c.execute(f'SELECT fid,object_id,feature_revision FROM managed."{database.table(d["dataset_id"])}"');self.assertEqual(c.fetchall(),identities)
            restored='restored_'+uuid.uuid4().hex
            fixture.CLUSTER.tool('createdb',restored)
            fixture.CLUSTER.tool('psql','-X','-v','ON_ERROR_STOP=1','-d',restored,'-f',dump)
            restore=connect(restored)
            try:
                self.assertEqual(database.export_schema(restore,d['dataset_id']),before)
                migrate(restore);self.assertEqual(database.export_schema(restore,d['dataset_id']),upgraded)
                database.apply_rows(restore,before,[row])
            finally:restore.close()

    def test_relation_revision_rejects_existing_orphan_and_cross_group_target(self):
        parent=self.dataset({'schema_version':1,'geometry':None,'fields':[{'name':'x','type':'int32','nullable':False}]})
        value=definition();value['fields'].append({'name':'parent','type':'uuid','nullable':True,'default':None})
        child=self.dataset(value,version_group_id=parent['version_group_id'])
        row=self.row2({'parent':str(uuid.uuid4())});database.apply_rows(self.connection,child,[row]);child=self.refresh(child)
        value['relationships']=[{'name':'parent_ref','field':'parent','target_dataset':parent['dataset_id'],'cardinality':'many-to-one','on_delete':'restrict'}]
        before=self.state(child)
        with self.assertRaises(psycopg2.Error):database.revise_schema(self.connection,child,value)
        self.assertEqual(self.state(child),before)
        foreign=self.dataset()
        value['relationships'][0]['target_dataset']=foreign['dataset_id']
        with self.assertRaisesRegex(ValueError,'same group'):self.dataset(value,version_group_id=parent['version_group_id'])
        self.assertEqual(self.state(child),before)

    def test_group_constraints_fail_inside_call_and_no_raw_execution(self):
        parent=self.dataset({'schema_version':1,'geometry':None,'fields':[{'name':'x','type':'int32','nullable':False}]})
        value=definition();value['fields'].append({'name':'parent','type':'uuid','nullable':False})
        value['relationships']=[{'name':'parent_ref','field':'parent','target_dataset':parent['dataset_id'],'cardinality':'many-to-one','on_delete':'restrict'}]
        child=self.dataset(value,version_group_id=parent['version_group_id'])
        descriptor=self.refresh(child);before=self.state(child)
        # Catch at execute time, not just Python context manager COMMIT time.
        with self.connection,self.connection.cursor() as cursor:
            with self.assertRaises(psycopg2.Error):
                import json
                edits=[{'dataset_id':child['dataset_id'],'rows':[self.row2({'parent':str(uuid.uuid4())})],'replace':False}]
                cursor.execute('SELECT managed.apply_group(%s,%s,%s::jsonb)',(descriptor['current']['version_id'],descriptor['current']['head_revision'],json.dumps(edits)))
        self.assertEqual(before,self.state(child))
        for edits in ([],[{}],[{'dataset_id':child['dataset_id'],'rows':[],'replace':None}],
                      [{'dataset_id':child['dataset_id'],'rows':[],'replace':False}]*2):
            with self.assertRaises(psycopg2.Error):database.apply_group(self.connection,self.refresh(child),edits)
            self.assertEqual(before,self.state(child))

    def test_group_and_schema_change_serialize_with_stale_head(self):
        import concurrent.futures
        import threading
        import time
        d=self.dataset(definition());ready=threading.Event()
        first=connect()
        try:
            with first.cursor() as c:
                c.execute('SELECT 1 FROM managed.version_group WHERE group_id=%s FOR UPDATE',(d['version_group_id'],))
                c.execute("SET application_name='db02-schema-holder'")
            def revise():
                conn=connect()
                try:
                    with conn,conn.cursor() as c:c.execute("SET application_name='db02-schema-waiter'")
                    ready.set();value=definition();value['fields'][1]['default']='changed'
                    return database.revise_schema(conn,d,value)
                finally:conn.close()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future=pool.submit(revise);self.assertTrue(ready.wait(2))
                import test_managed_database as fixture
                fixture.CLUSTER.wait_activity('db02-schema-waiter','transactionid');self.assertFalse(future.done())
                # Same connection already owns the lock; raw SQL is used only by
                # this trusted fixture to exercise the actual service primitive.
                with first.cursor() as c:
                    c.execute('SELECT managed.apply_rows(%s,%s,%s,%s::jsonb,false)',(d['dataset_id'],d['current']['version_id'],d['current']['head_revision'],'[]'))
                first.commit()
                with self.assertRaisesRegex(ValueError,'STALE_SCHEMA'):future.result(timeout=10)
            self.assertEqual(self.refresh(d)['schema_revision_id'],d['schema_revision_id'])
        finally:first.close()

    def test_typed_rules_literals_unknown_operators_and_null_semantics(self):
        value=definition()
        value['rules'].append({'name':'required_status','kind':'required','field':'status'})
        d=self.dataset(value);self.rejects(d,[self.row2({'status':None})])
        value=definition();value['rules'].append({'name':'status_literal','kind':'compare','field':'status','operator':'eq','operand':{'literal':'new'}})
        d=self.dataset(value);self.rejects(d,[self.row2({'status':'old'})]);self.rejects(d,[self.row2({'status':None})])
        value=definition();value['fields'][1]['default']="literal quote '; SELECT 1; --"
        d=self.dataset(value);database.apply_rows(self.connection,d,[self.row2()])
        self.assertEqual(self.query(f'SELECT label FROM managed."{database.table(d["dataset_id"])}"'),[(value['fields'][1]['default'],)])
        for change in ('sql','python','arcade','regex'):
            invalid=definition();invalid['rules'][0]['operator']=change
            with self.assertRaises(ValueError):self.dataset(invalid)

    def test_exact_decimal_date_and_literal_comparison_constraints(self):
        from test_schema import SchemaRulesTests
        value=SchemaRulesTests().definition()
        value['fields'].append({'name':'when_date','type':'date','nullable':False,'default':'2026-10-03',
            'domain':{'kind':'range','min':'2026-01-01','max':'2026-12-31'}})
        value['rules']=[{'name':'exact_max','kind':'compare','field':'value','operator':'eq',
            'operand':{'literal':value['fields'][1]['default']}}]
        d=self.dataset(value);row=self.row2()
        database.apply_rows(self.connection,d,[row]);d=self.refresh(d)
        self.assertEqual(self.query(f'SELECT value::text FROM managed."{database.table(d["dataset_id"])}"'),
                         [(value['fields'][1]['default'],)])
        self.rejects(d,[self.row2({'value':'999999999999999999999999999999999999.98'})])
        self.rejects(d,[self.row2({'when_date':'2027-01-01'})])
        self.rejects(d,[self.row2({'when_date':'2026-02-30'})])

    def related_group(self):
        parent=self.dataset({'schema_version':1,'geometry':None,'fields':[{'name':'x','type':'int32','nullable':False}]})
        value=definition();value['fields'].append({'name':'parent','type':'uuid','nullable':False})
        value['relationships']=[{'name':'ref','field':'parent','target_dataset':parent['dataset_id'],'cardinality':'one-to-one','on_delete':'restrict'}]
        child=self.dataset(value,version_group_id=parent['version_group_id'])
        parents=[{'fid':str(uuid.uuid4()),'values':{'x':i}} for i in (1,2)]
        children=[self.row2({'parent':p['fid']}) for p in parents]
        database.apply_group(self.connection,self.refresh(child),[
            {'dataset_id':parent['dataset_id'],'rows':parents,'replace':False},
            {'dataset_id':child['dataset_id'],'rows':children,'replace':False}])
        constraints=self.query("SELECT conname FROM pg_constraint WHERE conrelid=%s::regclass AND condeferrable ORDER BY conname",('managed.'+database.table(child['dataset_id']),))
        return child,parents,children,[name for (name,) in constraints]

    def test_constraint_checks_do_not_flush_unrelated_group_work(self):
        group_a=self.dataset(definition());group_b,parents,children,constraints=self.related_group()
        descriptor=self.refresh(group_a)
        # Trusted fixture stages a temporarily invalid B state. A must not
        # change B constraint timing or flush B pending deferred checks.
        try:
            with self.connection.cursor() as cursor:
                for name in constraints:cursor.execute('SET CONSTRAINTS managed."'+name+'" DEFERRED')
                cursor.execute('UPDATE managed."'+database.table(group_b['dataset_id'])+'" SET parent=%s',(parents[0]['fid'],))
                cursor.execute('SELECT managed.apply_rows(%s,%s,%s,%s::jsonb,false)',
                    (group_a['dataset_id'],descriptor['current']['version_id'],descriptor['current']['head_revision'],'[]'))
                self.assertIsNotNone(cursor.fetchone()[0])
        finally:self.connection.rollback()
        self.assertEqual(self.refresh(group_a)['current'],descriptor['current'])

    def test_unrelated_schema_ddl_does_not_block_group_edit(self):
        import concurrent.futures
        group_a=self.dataset(definition());group_b,parents,children,constraints=self.related_group()
        other=connect()
        try:
            with other.cursor() as cursor:
                cursor.execute('SELECT 1 FROM managed.version_group WHERE group_id=%s FOR UPDATE',(group_b['version_group_id'],))
                cursor.execute('ALTER TABLE managed."'+database.table(group_b['dataset_id'])+'" DROP CONSTRAINT "'+constraints[0]+'"')
            def edit_a():
                conn=connect()
                try:return database.apply_rows(conn,group_a,[self.row2()])
                finally:conn.close()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result=pool.submit(edit_a)
                try:self.assertIsNotNone(result.result(timeout=3))
                finally:other.rollback()
            self.assertEqual(len(self.mapping(group_a)),1)
        finally:other.close()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(SchemaRulesDatabaseTests(name) for name in SchemaRulesDatabaseTests.__dict__ if name.startswith("test_"))
