# SPDX-License-Identifier: GPL-3.0-or-later
"""Real owned PostgreSQL/PostGIS acceptance; missing harness is a failure."""
import concurrent.futures
import contextlib
import copy
import json
from pathlib import Path
import random
import tempfile
import threading
import time
import unittest
import uuid
import psycopg2
from managed.database import apply_rows, create_dataset, export_identity, export_schema, grant_service, table
from managed.migrate import LOCK, MIGRATIONS, migrate
from managed.schema import fingerprint, object_id_int32
from test_schema import DEFINITION

CLUSTER = None
BASELINE = False


def connect(database='prototype'):
    if CLUSTER is None: raise RuntimeError('Run tools/run_managed_acceptance.py against the owned disposable fixture')
    return psycopg2.connect(host=str(CLUSTER.socket),port=CLUSTER.env['PGPORT'],user=CLUSTER.env['PGUSER'],dbname=database)


class ManagedDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.connection=connect()
        if not BASELINE: migrate(self.connection)

    def tearDown(self):
        self.connection.close()

    def dataset(self, definition=None, **kwargs):
        return create_dataset(self.connection, managed_name='d_'+uuid.uuid4().hex,
            catalog_item_id=str(uuid.uuid4()),policy_ref=str(uuid.uuid4()),
            definition=copy.deepcopy(DEFINITION if definition is None else definition),**kwargs)

    def refresh(self,d):
        return export_schema(self.connection,d['dataset_id'])

    def row(self, **kwargs):
        row={'fid':str(uuid.uuid4()),'values':{'name':'München','amount':'1.125'},'geometry':'SRID=4326;POINT(1 2)'}
        row.update(kwargs);return row

    def query(self, statement, parameters=()):
        with self.connection:
            with self.connection.cursor() as c:
                c.execute(statement,parameters)
                return c.fetchall() if c.description else []

    def mapping(self,d):
        return dict(self.query('SELECT fid::text,object_id FROM managed.feature_identity WHERE dataset_id=%s ORDER BY fid',(d['dataset_id'],)))

    def state(self,d):
        return (self.refresh(d)['current'],self.mapping(d),self.query(f'SELECT row_to_json(t)::text FROM managed."{table(d["dataset_id"])}" t ORDER BY fid'))

    def rejects(self,d,rows,expected=None):
        before=self.state(d)
        with self.assertRaises(psycopg2.Error) as error: apply_rows(self.connection,self.refresh(d),rows,replace=True)
        if expected: self.assertIn(expected,str(error.exception))
        self.assertEqual(before,self.state(d))

    @contextlib.contextmanager
    def empty_database(self):
        name='managed_'+uuid.uuid4().hex
        CLUSTER.tool('createdb',name)
        connection=connect(name)
        try:
            with connection:
                with connection.cursor() as c:c.execute('CREATE EXTENSION postgis')
            yield connection
        finally: connection.close()

    def test_migration_reapply_checksum_and_failure_atomicity(self):
        first=migrate(self.connection);self.assertEqual(first,migrate(self.connection))
        with tempfile.TemporaryDirectory() as td:
            directory=Path(td)
            (directory/'001_managed.sql').write_text((MIGRATIONS/'001_managed.sql').read_text()+'\n-- modified\n')
            with self.assertRaisesRegex(ValueError,'IDENTITY_MISMATCH'):migrate(self.connection,directory)
            (directory/'001_managed.sql').write_bytes((MIGRATIONS/'001_managed.sql').read_bytes())
            (directory/'002_failure.sql').write_text('CREATE TABLE managed.partial_migration(x int); SELECT 1/0;')
            with self.assertRaises(psycopg2.Error):migrate(self.connection,directory)
        self.assertEqual(self.query("SELECT to_regclass('managed.partial_migration')"),[(None,)])
        self.assertEqual(self.query('SELECT version FROM managed.migration_history'),[(1,)])

    def test_failed_initial_install_leaves_no_schema(self):
        with self.empty_database() as conn, tempfile.TemporaryDirectory() as td:
            p=Path(td)/'001_failure.sql';p.write_text('CREATE TABLE managed.partial(x int); SELECT 1/0;')
            with self.assertRaises(psycopg2.Error):migrate(conn,p.parent)
            with conn,conn.cursor() as c:
                c.execute("SELECT to_regnamespace('managed')");self.assertIsNone(c.fetchone()[0])

    def test_utf8_prerequisite_and_successful_forward_migration(self):
        name='ascii_'+uuid.uuid4().hex
        CLUSTER.tool('createdb','--template=template0','--encoding=SQL_ASCII',name)
        conn=connect(name)
        try:
            with self.assertRaisesRegex(ValueError,'UTF8_DATABASE_REQUIRED'):migrate(conn)
            with conn,conn.cursor() as c:
                c.execute("SELECT to_regnamespace('managed')");self.assertIsNone(c.fetchone()[0])
        finally:conn.close()
        with self.empty_database() as conn,tempfile.TemporaryDirectory() as td:
            directory=Path(td);(directory/'001_managed.sql').write_bytes((MIGRATIONS/'001_managed.sql').read_bytes())
            (directory/'002_forward.sql').write_text('CREATE TABLE managed.forward_probe(value integer NOT NULL); INSERT INTO managed.forward_probe VALUES(23);')
            result=migrate(conn,directory);self.assertEqual(len(result),2);self.assertEqual(result,migrate(conn,directory))
            with conn,conn.cursor() as c:
                c.execute('SELECT value FROM managed.forward_probe');self.assertEqual(c.fetchall(),[(23,)])
            with self.assertRaisesRegex(ValueError,'IDENTITY_MISMATCH'):migrate(conn)

    def test_prototype_and_unmarked_schema_are_not_adopted(self):
        with self.empty_database() as conn:
            with conn,conn.cursor() as c:c.execute('CREATE SCHEMA geodb; CREATE TABLE geodb.sentinel(x integer); INSERT INTO geodb.sentinel VALUES(7)')
            with self.assertRaisesRegex(ValueError,'PROTOTYPE_ADOPTION_UNSUPPORTED'):migrate(conn)
            with conn,conn.cursor() as c:
                c.execute('SELECT x FROM geodb.sentinel');self.assertEqual(c.fetchall(),[(7,)])
        with self.empty_database() as conn:
            with conn,conn.cursor() as c:c.execute('CREATE SCHEMA managed; CREATE TABLE managed.sentinel(x integer)')
            with self.assertRaisesRegex(ValueError,'UNMARKED_SCHEMA'):migrate(conn)

    def test_concurrent_migrations_wait_and_apply_once(self):
        with self.empty_database() as conn:
            db=conn.info.dbname
            with conn.cursor() as c:c.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK,))
            barrier=threading.Barrier(3)
            def worker():
                other=connect(db)
                try:barrier.wait();return migrate(other)
                finally:other.close()
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                tasks=[pool.submit(worker) for _ in range(2)];barrier.wait()
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    count=self.query("SELECT count(*) FROM pg_stat_activity WHERE datname=%s AND wait_event='advisory'",(db,))[0][0]
                    if count==2:break
                    time.sleep(.02)
                self.assertEqual(count,2);self.assertFalse(any(t.done() for t in tasks))
                conn.commit();self.assertEqual(tasks[0].result(10),tasks[1].result(10))
            with conn,conn.cursor() as c:
                c.execute('SELECT version FROM managed.migration_history');self.assertEqual(c.fetchall(),[(1,)])

    def test_reorder_republish_delete_reinsert_preserves_identity(self):
        d=self.dataset();rows=[self.row(values={'name':str(i),'amount':'0.000'}) for i in range(40)]
        apply_rows(self.connection,d,rows,replace=True);original=self.mapping(d)
        physical=self.query('SELECT %s::regclass::oid',('managed.'+table(d['dataset_id']),))[0][0]
        random.Random(90210).shuffle(rows)
        apply_rows(self.connection,self.refresh(d),rows,replace=True);self.assertEqual(self.mapping(d),original)
        apply_rows(self.connection,self.refresh(d),rows[:10],replace=True)
        self.assertEqual(self.mapping(d),original)
        new=self.row();apply_rows(self.connection,self.refresh(d),[new],replace=True)
        self.assertGreater(self.mapping(d)[new['fid']],max(original.values()))
        apply_rows(self.connection,self.refresh(d),rows,replace=True)
        self.assertTrue(all(self.mapping(d)[fid]==oid for fid,oid in original.items()))
        self.assertEqual(self.query('SELECT %s::regclass::oid',('managed.'+table(d['dataset_id']),))[0][0],physical)
        self.assertEqual(self.refresh(d)['schema_sha256'],d['schema_sha256'])

    def test_import_uuid_namespace_is_per_dataset_not_row_position(self):
        a,b=self.dataset(),self.dataset();row=self.row()
        apply_rows(self.connection,a,[row]);apply_rows(self.connection,b,[row])
        self.assertEqual(self.mapping(a),{row['fid']:1});self.assertEqual(self.mapping(b),{row['fid']:1})
        bad=self.row();bad.pop('fid');self.rejects(a,[bad])
        self.rejects(a,[row,row],'DUPLICATE_FEATURE_UUID')

    def test_stale_concurrent_allocation_and_retry(self):
        d=self.dataset();rows=[self.row(),self.row()];barrier=threading.Barrier(3)
        def worker(row):
            conn=connect()
            try:
                barrier.wait()
                try:return ('ok',apply_rows(conn,d,[row]))
                except psycopg2.Error as e:return ('error',str(e))
            finally:conn.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs=[pool.submit(worker,row) for row in rows];barrier.wait();results=[j.result(10) for j in jobs]
        self.assertEqual(sum(k=='ok' for k,_ in results),1)
        self.assertIn('STALE_HEAD',next(v for k,v in results if k=='error'))
        missing=next(row for row in rows if row['fid'] not in self.mapping(d))
        apply_rows(self.connection,self.refresh(d),[missing]);self.assertEqual(sorted(self.mapping(d).values()),[1,2])

    def test_invalid_batch_rolls_back_rows_registry_and_head(self):
        d=self.dataset();original=self.row();apply_rows(self.connection,d,[original])
        invalid=self.row(values={'name':'123456789','amount':'1.125'})
        self.rejects(d,[self.row(),invalid])
        self.rejects(d,[self.row(values={'name':'abc','amount':'1.1254'})])
        self.rejects(d,[self.row(values={'name':'abc','amount':'1000000000'})])

    def test_exact_decimal_and_unicode_length_database_constraints(self):
        d=self.dataset();r=self.row(values={'name':'東京😀abcdΩ','amount':'-999999999.999'})
        apply_rows(self.connection,d,[r]);t=table(d['dataset_id'])
        self.assertEqual(self.query(f'SELECT name,amount::text FROM managed."{t}"'),[('東京😀abcdΩ','-999999999.999')])
        for expression in ["amount=1.0001","amount='NaN'::numeric","amount='Infinity'::numeric","name='123456789'","name='12345678 '"]:
            with self.assertRaises(psycopg2.Error):self.query(f'UPDATE managed."{t}" SET '+expression)
        self.rejects(d,[self.row(values={'name':'','amount':1.125})],'INVALID_TYPED_VALUE')
        apply_rows(self.connection,self.refresh(d),[self.row(values={'name':'','amount':'1.2300'}),self.row(values={'name':None,'amount':'0'})])
        self.assertEqual(self.query(f'SELECT count(*) FROM managed."{t}" WHERE name IS NULL OR name=\'\''),[(2,)])

    def test_all_scalar_types_and_lossless_native_oid(self):
        definition={'schema_version':1,'geometry':None,'fields':[
            {'name':'small','type':'int32','nullable':False}, {'name':'large','type':'int64','nullable':False},
            {'name':'flag','type':'boolean','nullable':False}, {'name':'related','type':'uuid','nullable':True},
            {'name':'day','type':'date','nullable':False}, {'name':'instant','type':'timestamp','nullable':False}]}
        d=self.dataset(definition);fid=str(uuid.uuid4());v={'small':2147483647,'large':'9223372036854775807','flag':True,'related':None,'day':'2024-02-29','instant':'2024-01-01T01:30:00.123456+01:30'}
        row={'fid':fid,'values':v};apply_rows(self.connection,d,[row]);t=table(d['dataset_id'])
        result=self.query(f'SELECT small,large,flag,related,day::text,instant AT TIME ZONE \'UTC\' FROM managed."{t}"')[0]
        self.assertEqual(result[:5],(2147483647,9223372036854775807,True,None,'2024-02-29'))
        self.assertEqual(result[5].isoformat(),'2024-01-01T00:00:00.123456')
        for key,value in [('small',2147483648),('small',True),('large',9223372036854775807),('large','9223372036854775808'),('flag','true'),('day','2023-02-29'),('instant','2024-01-01T24:00:00Z'),('instant','2024-01-01T00:00:00.1234567Z'),('instant','2024-01-01T00:00:00'),('related','bad')]:
            invalid=copy.deepcopy(row);invalid['values'][key]=value
            with self.subTest(key=key,value=value):self.rejects(d,[invalid])
        sequence='managed.oid_'+uuid.UUID(d['dataset_id']).hex
        self.query('SELECT setval(%s::regclass,2147483647,true)',(sequence,))
        row2=copy.deepcopy(row);row2['fid']=str(uuid.uuid4());apply_rows(self.connection,self.refresh(d),[row2])
        actual=self.query(f'SELECT fid::text,object_id,feature_revision::text FROM managed."{t}" WHERE fid=%s',(row2['fid'],))[0]
        self.assertEqual(export_identity(*actual)['object_id'],'2147483648')
        with self.assertRaisesRegex(ValueError,'COMPATIBILITY_OVERFLOW'):object_id_int32(actual[1])
        self.query('SELECT setval(%s::regclass,9223372036854775807,true)',(sequence,))
        row3=copy.deepcopy(row);row3['fid']=str(uuid.uuid4());self.rejects(d,[row3],'maximum value')

    def test_geometry_srid_type_dimensions_4326_2230(self):
        for srid in (4326,2230):
            for dimensions,suffix,coordinates in [('XY','','1 2'),('XYZ','Z','1 2 3'),('XYM','M','1 2 4'),('XYZM','ZM','1 2 3 4')]:
                with self.subTest(srid=srid,dimensions=dimensions):
                    definition=copy.deepcopy(DEFINITION);definition['geometry'].update(srid=srid,dimensions=dimensions)
                    d=self.dataset(definition);row=self.row(geometry=f'SRID={srid};POINT {suffix}({coordinates})')
                    apply_rows(self.connection,d,[row]);t=table(d['dataset_id'])
                    self.assertEqual(self.query(f'SELECT public.ST_SRID(geom),public.ST_Zmflag(geom) FROM managed."{t}"'),[(srid,{'XY':0,'XYZ':2,'XYM':1,'XYZM':3}[dimensions])])
                    self.rejects(d,[self.row(geometry=f'SRID=0;POINT {suffix}({coordinates})')])
                    self.rejects(d,[self.row(geometry=f'SRID={2230 if srid==4326 else 4326};POINT {suffix}({coordinates})')])
                    wrong='Z(1 2 3)' if dimensions=='XY' else '(1 2)'
                    self.rejects(d,[self.row(geometry=f'SRID={srid};POINT {wrong}')])
                    self.rejects(d,[self.row(geometry=f'SRID={srid};MULTIPOINT {suffix}(({coordinates}))')])

    def test_geometry_null_empty_invalid_nonfinite(self):
        d=self.dataset()
        for geometry in [None,'SRID=4326;POINT EMPTY','SRID=4326;POINT(NaN 2)','SRID=4326;POINT(Infinity 2)','POINT(1 2)']:
            self.rejects(d,[self.row(geometry=geometry)])
        definition=copy.deepcopy(DEFINITION);definition['geometry'].update(nullable=True,allow_empty=True)
        allowed=self.dataset(definition)
        apply_rows(self.connection,allowed,[self.row(geometry=None),self.row(geometry='SRID=4326;POINT EMPTY')])
        self.assertEqual(self.query(f'SELECT count(*) FROM managed."{table(allowed["dataset_id"])}" WHERE geom IS NULL OR public.ST_IsEmpty(geom)'),[(2,)])
        for dimensions,geometry in [('XYZ','POINT Z(1 2 NaN)'),('XYM','POINT M(1 2 Infinity)'),('XYZM','POINT ZM(1 2 3 NaN)')]:
            definition['geometry'].update(dimensions=dimensions)
            target=self.dataset(definition);self.rejects(target,[self.row(geometry='SRID=4326;'+geometry)])
        definition['geometry'].update(type='Polygon',dimensions='XY');polygon=self.dataset(definition)
        self.rejects(polygon,[self.row(geometry='SRID=4326;POLYGON((0 0,2 2,0 2,2 0,0 0))')])

    def test_default_group_and_cross_dataset_constraints(self):
        first=self.dataset();second=self.dataset(version_group_id=first['version_group_id']);foreign=self.dataset()
        self.assertEqual(first['default_version_id'],second['default_version_id'])
        self.assertNotEqual(first['current']['head_revision'],second['current']['head_revision'])
        self.assertEqual(second['group_schema_generation'],2)
        with self.assertRaisesRegex(psycopg2.Error,'STALE_HEAD'):apply_rows(self.connection,first,[self.row()])
        row=self.row();apply_rows(self.connection,self.refresh(first),[row]);t=table(first['dataset_id'])
        for statement,params in [(f'UPDATE managed."{t}" SET version_id=%s',(foreign['default_version_id'],)),
                                  (f'UPDATE managed."{t}" SET dataset_id=%s',(foreign['dataset_id'],)),
                                  (f'UPDATE managed."{t}" SET object_id=999',())]:
            with self.assertRaises(psycopg2.Error):self.query(statement,params)
        with self.assertRaises(psycopg2.Error):self.query("INSERT INTO managed.version VALUES(%s,%s,'named',%s)",(str(uuid.uuid4()),first['version_group_id'],str(uuid.uuid4())))
        changed=copy.deepcopy(self.refresh(first));changed['current']['version_id']=foreign['default_version_id']
        with self.assertRaisesRegex(psycopg2.Error,'DATASET_VERSION_MISMATCH'):apply_rows(self.connection,changed,[row])

    def test_schema_hash_immutable_and_policy_is_only_a_reference(self):
        d=self.dataset();definition=copy.deepcopy(DEFINITION)
        self.assertEqual(d['schema_sha256'],fingerprint(definition));self.assertEqual(d['identity']['feature_revision_field'],'feature_revision')
        self.query('UPDATE managed.dataset SET policy_ref=%s WHERE dataset_id=%s',(str(uuid.uuid4()),d['dataset_id']))
        self.assertEqual(self.refresh(d)['schema_sha256'],d['schema_sha256'])
        for statement in ['UPDATE managed.schema_revision SET canonical_text=canonical_text','DELETE FROM managed.schema_revision','UPDATE managed.dataset SET dataset_id=dataset_id']:
            with self.assertRaisesRegex(psycopg2.Error,'IMMUTABLE_MANAGED_IDENTITY'):self.query(statement)
        row=self.row();apply_rows(self.connection,self.refresh(d),[row]);before=self.query(f'SELECT feature_revision FROM managed."{table(d["dataset_id"])}"')[0][0]
        apply_rows(self.connection,self.refresh(d),[row]);after=self.query(f'SELECT feature_revision FROM managed."{table(d["dataset_id"])}"')[0][0]
        self.assertNotEqual(before,after)
        with self.assertRaisesRegex(psycopg2.Error,'IMMUTABLE_MANAGED_IDENTITY'):self.query('DELETE FROM managed.feature_identity WHERE dataset_id=%s',(d['dataset_id'],))

    def test_real_export_and_physical_types(self):
        d=self.dataset();row=self.row();apply_rows(self.connection,d,[row]);d=self.refresh(d)
        self.assertEqual(d['definition'],DEFINITION)
        types=dict(self.query('SELECT attname,format_type(atttypid,atttypmod) FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped',('managed.'+table(d['dataset_id']),)))
        self.assertEqual({k:types[k] for k in ['name','amount','geom','fid','object_id','version_id','feature_revision']},
                         {'name':'text','amount':'numeric','geom':'geometry','fid':'uuid','object_id':'bigint','version_id':'uuid','feature_revision':'uuid'})
        actual=self.query(f'SELECT fid::text,object_id,feature_revision::text FROM managed."{table(d["dataset_id"])}"')[0]
        (CLUSTER.evidence/'managed-export.json').write_text(json.dumps({'dataset':d,'feature_identity':export_identity(*actual),'physical_types':types},indent=2,sort_keys=True)+'\n')

    def test_ordinary_roles_cannot_bypass_managed_writes(self):
        d=self.dataset();writer='writer_'+uuid.uuid4().hex;reader='reader_'+uuid.uuid4().hex
        self.query(f'CREATE ROLE "{writer}"; CREATE ROLE "{reader}"')
        grant_service(self.connection,writer)
        self.query(f'CREATE VIEW managed."view_{uuid.UUID(d["dataset_id"]).hex}" AS SELECT fid,object_id,name FROM managed."{table(d["dataset_id"])}"')
        self.query(f'GRANT USAGE ON SCHEMA managed TO "{reader}"; GRANT SELECT ON managed."view_{uuid.UUID(d["dataset_id"]).hex}" TO "{reader}"')
        for role in ('outsider',reader,writer):
            conn=connect()
            try:
                with conn,conn.cursor() as c:c.execute(f'SET ROLE "{role}"')
                if role==writer:
                    # Attacker-controlled temporary names/search_path must not
                    # redirect the security-definer registry or version reads.
                    with conn,conn.cursor() as c:c.execute('CREATE TEMP TABLE dataset(dataset_id uuid); CREATE TEMP TABLE version(version_id uuid); SET search_path=pg_temp,public')
                    apply_rows(conn,self.refresh(d),[self.row()])
                else:
                    with self.assertRaises(psycopg2.Error):apply_rows(conn,self.refresh(d),[self.row()])
                statements=[f'DELETE FROM managed."{table(d["dataset_id"])}"',f'TRUNCATE managed."{table(d["dataset_id"])}"',
                            'UPDATE managed.feature_identity SET object_id=42','DELETE FROM managed.dataset',
                            'CREATE TABLE managed.bypass(x int)',f"SELECT setval('managed.oid_{uuid.UUID(d['dataset_id']).hex}',1)"]
                for sql in statements:
                    with self.subTest(role=role,sql=sql),self.assertRaises(psycopg2.Error):
                        with conn,conn.cursor() as c:c.execute(sql)
                if role==reader:
                    with conn,conn.cursor() as c:
                        c.execute(f'SELECT * FROM managed."view_{uuid.UUID(d["dataset_id"]).hex}"');c.fetchall()
            finally:conn.close()

    def test_request_shape_null_and_schema_creation_failure(self):
        d=self.dataset()
        for rows in [None,{},[None],[{}],[self.row(values={'name':'a'})],[self.row(values={'name':'a','amount':'1','extra':1})]]:
            self.rejects(d,rows)
        self.rejects(d,[self.row(values={'name':'a','amount':None})],'NULL_NOT_ALLOWED')
        for field in ('version_id','head_revision'):
            invalid=copy.deepcopy(self.refresh(d));invalid['current'][field]=None
            with self.assertRaisesRegex(psycopg2.Error,'INVALID_MANAGED_REQUEST'):apply_rows(self.connection,invalid,[self.row()])
        with self.assertRaisesRegex(psycopg2.Error,'INVALID_MANAGED_REQUEST'):apply_rows(self.connection,self.refresh(d),[self.row()],replace=None)
        before=self.query('SELECT count(*) FROM managed.dataset')[0][0]
        definition=copy.deepcopy(DEFINITION);definition['geometry']['srid']=998998
        with self.assertRaisesRegex(ValueError,'unknown retained spatial'):self.dataset(definition)
        self.assertEqual(before,self.query('SELECT count(*) FROM managed.dataset')[0][0])

    def test_library_does_not_commit_callers_existing_transaction(self):
        d=self.dataset()
        with self.connection.cursor() as c:c.execute('CREATE TEMP TABLE caller_pending(x int); INSERT INTO caller_pending VALUES(1)')
        for operation in (lambda:migrate(self.connection),lambda:self.refresh(d),lambda:apply_rows(self.connection,d,[])):
            with self.assertRaisesRegex(ValueError,'idle transactional connection'):operation()
        self.connection.rollback()
        self.assertEqual(self.query("SELECT to_regclass('pg_temp.caller_pending')"),[(None,)])

    def test_service_grant_rejects_direct_inherited_and_owner_bypass(self):
        definition=copy.deepcopy(DEFINITION);definition['geometry']=None
        d=self.dataset(definition);t=table(d['dataset_id'])
        suffix=uuid.uuid4().hex
        direct='direct_'+suffix;column='column_'+suffix;parent='parent_'+suffix;child='child_'+suffix
        seq='seq_'+suffix;owner='owner_'+suffix;function='function_'+suffix
        for role in (direct,column,parent,child,seq,owner,function):self.query(f'CREATE ROLE "{role}" NOINHERIT')
        self.query(f'GRANT UPDATE ON managed."{t}" TO "{direct}"')
        self.query(f'GRANT UPDATE(name) ON managed."{t}" TO "{column}"')
        self.query(f'GRANT DELETE ON managed."{t}" TO "{parent}"; GRANT "{parent}" TO "{child}"')
        self.assertEqual(self.query('SELECT has_table_privilege(%s,%s,\'DELETE\'),pg_has_role(%s,%s,\'MEMBER\')',(child,'managed.'+t,child,parent)),[(False,True)])
        self.query(f'GRANT UPDATE ON SEQUENCE managed."oid_{uuid.UUID(d["dataset_id"]).hex}" TO "{seq}"')
        self.query(f'GRANT prototype_owner TO "{owner}"')
        self.query(f'GRANT EXECUTE ON FUNCTION managed.geometry_finite(public.geometry) TO "{function}"')
        for role in (direct,column,child,seq,owner,function):
            before=self.query("SELECT has_function_privilege(%s,'managed.apply_rows(uuid,uuid,uuid,jsonb,boolean)','EXECUTE')",(role,))
            with self.subTest(role=role),self.assertRaisesRegex(ValueError,'backend role has'):grant_service(self.connection,role)
            self.assertEqual(self.query("SELECT has_function_privilege(%s,'managed.apply_rows(uuid,uuid,uuid,jsonb,boolean)','EXECUTE')",(role,)),before)

    def test_owner_cannot_be_mistaken_for_safe_role_after_acl_revocation(self):
        suffix=uuid.uuid4().hex;role='revoked_'+suffix;child='member_'+suffix;owned='owned_'+suffix
        self.query(f'CREATE ROLE "{role}" NOINHERIT; CREATE ROLE "{child}" NOINHERIT; GRANT "{role}" TO "{child}"')
        self.query(f'CREATE TABLE managed."{owned}" (value integer); ALTER TABLE managed."{owned}" OWNER TO "{role}"; REVOKE ALL ON managed."{owned}" FROM "{role}"')
        self.assertEqual(self.query('SELECT has_table_privilege(%s,%s,\'UPDATE\'),has_schema_privilege(%s,\'managed\',\'CREATE\')',(role,'managed.'+owned,role)),[(False,False)])
        for actor in (role,child):
            with self.assertRaisesRegex(ValueError,'existing managed bypass'):grant_service(self.connection,actor)
            self.assertEqual(self.query("SELECT has_function_privilege(%s,'managed.apply_rows(uuid,uuid,uuid,jsonb,boolean)','EXECUTE')",(actor,)),[(False,)])

    def test_server_capability_memberships_are_not_backend_roles(self):
        # Membership checks only: never execute a program or read/write files.
        for capability in ('pg_read_server_files','pg_write_server_files','pg_execute_server_program','pg_signal_backend','pg_read_all_data','pg_write_all_data','pg_checkpoint'):
            role='cap_'+uuid.uuid4().hex
            self.query(f'CREATE ROLE "{role}" NOINHERIT; GRANT "{capability}" TO "{role}"')
            with self.subTest(capability=capability),self.assertRaisesRegex(ValueError,'privileged membership'):grant_service(self.connection,role)
            self.assertEqual(self.query("SELECT has_function_privilege(%s,'managed.apply_rows(uuid,uuid,uuid,jsonb,boolean)','EXECUTE')",(role,)),[(False,)])
