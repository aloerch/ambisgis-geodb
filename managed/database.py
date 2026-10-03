# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted migration/schema operations and the finite service write primitive.

Connections are dedicated to these operations. Catalog authorization belongs to
the service before invocation; catalog UUIDs in this registry confer no access.
"""
import json
import uuid
from .schema import canonical, columns, fingerprint, identifier, normalized


def identity(value):
    return str(uuid.UUID(str(value)))


def table(dataset_id):
    return 'd_' + uuid.UUID(str(dataset_id)).hex


def require_idle(connection):
    if connection.autocommit or connection.get_transaction_status() != 0:
        raise ValueError('dedicated idle transactional connection required')


def create_dataset(connection, *, managed_name, catalog_item_id, policy_ref, definition,
                   dataset_id=None, version_group_id=None):
    require_idle(connection)
    definition = normalized(definition)
    managed_name = identifier(managed_name)
    dataset_id = identity(dataset_id or uuid.uuid4())
    schema_id = str(uuid.uuid4())
    group_id = identity(version_group_id or uuid.uuid4())
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='30s'; SET LOCAL timezone='UTC'")
            if version_group_id is None:
                version_id = str(uuid.uuid4())
                cursor.execute('INSERT INTO managed.version_group(group_id,default_version_id) VALUES(%s,%s)', (group_id,version_id))
                cursor.execute("INSERT INTO managed.version VALUES(%s,%s,'DEFAULT',%s)", (version_id,group_id,str(uuid.uuid4())))
            else:
                cursor.execute('SELECT default_version_id FROM managed.version_group WHERE group_id=%s FOR UPDATE', (group_id,))
                row = cursor.fetchone()
                if row is None: raise ValueError('unknown version group')
                version_id = str(row[0])
                cursor.execute('UPDATE managed.version SET head_revision=%s WHERE version_id=%s', (str(uuid.uuid4()),version_id))
                cursor.execute('UPDATE managed.version_group SET schema_generation=schema_generation+1 WHERE group_id=%s', (group_id,))
            if definition['geometry'] is not None:
                cursor.execute('SELECT 1 FROM public.spatial_ref_sys WHERE srid=%s', (definition['geometry']['srid'],))
                if cursor.fetchone() is None: raise ValueError('unknown retained spatial reference')
            cursor.execute('INSERT INTO managed.dataset VALUES(%s,%s,%s,%s,%s,%s)',
                           (dataset_id,managed_name,identity(catalog_item_id),identity(policy_ref),group_id,schema_id))
            text = canonical(definition)
            cursor.execute('INSERT INTO managed.schema_revision VALUES(%s,%s,%s,%s,%s::jsonb,%s)',
                           (schema_id,dataset_id,'ambisgis-typed-schema-json-v1',text,text,fingerprint(definition)))
            sequence = 'oid_' + uuid.UUID(dataset_id).hex
            cursor.execute(f'CREATE SEQUENCE managed."{sequence}" AS bigint MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE')
            fixed = [f"dataset_id uuid NOT NULL CHECK(dataset_id='{dataset_id}'::uuid)",
                     f"version_group_id uuid NOT NULL CHECK(version_group_id='{group_id}'::uuid)",
                     'version_id uuid NOT NULL', 'fid uuid NOT NULL', 'object_id bigint NOT NULL CHECK(object_id>0)',
                     'feature_revision uuid NOT NULL', 'PRIMARY KEY(version_id,fid)', 'UNIQUE(version_id,object_id)',
                     'FOREIGN KEY(dataset_id,version_group_id) REFERENCES managed.dataset(dataset_id,group_id)',
                     'FOREIGN KEY(version_group_id,version_id) REFERENCES managed.version(group_id,version_id)',
                     'FOREIGN KEY(dataset_id,fid,object_id) REFERENCES managed.feature_identity(dataset_id,fid,object_id)']
            cursor.execute(f'CREATE TABLE managed."{table(dataset_id)}" (' + ','.join(fixed + columns(definition)) + ')')
            if definition['geometry'] is not None:
                cursor.execute(f'CREATE INDEX ON managed."{table(dataset_id)}" USING gist(geom)')
            cursor.execute(f'REVOKE ALL ON TABLE managed."{table(dataset_id)}" FROM PUBLIC')
            cursor.execute(f'REVOKE ALL ON SEQUENCE managed."{sequence}" FROM PUBLIC')
    return export_schema(connection,dataset_id)


def export_schema(connection, dataset_id):
    require_idle(connection)
    with connection:
        with connection.cursor() as cursor:
            cursor.execute('''SELECT d.dataset_id,d.managed_name,d.catalog_item_id,d.policy_ref,d.group_id,
                g.default_version_id,r.schema_revision_id,r.schema_sha256,r.definition,g.schema_generation,v.head_revision
                FROM managed.dataset d JOIN managed.version_group g ON g.group_id=d.group_id
                JOIN managed.version v ON v.version_id=g.default_version_id
                JOIN managed.schema_revision r ON r.schema_revision_id=d.active_schema_revision WHERE d.dataset_id=%s''', (identity(dataset_id),))
            row = cursor.fetchone()
            if row is None: raise ValueError('unknown dataset')
    definition = normalized(row[8])
    if fingerprint(definition) != row[7]: raise ValueError('registered schema fingerprint mismatch')
    return {'schema_version': 1, 'contract': 'ambisgis-managed-dataset-v1', 'dataset_id': str(row[0]),
            'managed_name': row[1], 'catalog_item_id': str(row[2]), 'policy_ref': str(row[3]),
            'version_group_id': str(row[4]), 'default_version_id': str(row[5]), 'schema_revision_id': str(row[6]),
            'schema_sha256': row[7], 'canonical_encoding': 'ambisgis-typed-schema-json-v1',
            'definition': definition, 'group_schema_generation': row[9],
            'identity': {'feature_uuid_field': 'fid', 'object_id_field': 'object_id', 'object_id_type': 'int64',
                         'object_id_json_encoding': 'positive-decimal-string', 'never_recycled': True,
                         'feature_revision_field': 'feature_revision', 'feature_revision_type': 'uuid',
                         'version_field': 'version_id'},
            'current': {'version_id': str(row[5]), 'head_revision': str(row[10])}}


def apply_rows(connection, descriptor, rows, *, replace=False):
    require_idle(connection)
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='30s'; SET LOCAL timezone='UTC'")
            cursor.execute('SELECT managed.apply_rows(%s,%s,%s,%s::jsonb,%s)',
                           (descriptor['dataset_id'],descriptor['current']['version_id'],descriptor['current']['head_revision'],
                            json.dumps(rows,ensure_ascii=False,separators=(',',':'),allow_nan=False),replace))
            return str(cursor.fetchone()[0])


def grant_service(connection, role):
    """Deployment/migrator-only, for a trusted backend role; not end-user ACLs."""
    require_idle(connection)
    role = identifier(role)
    with connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT oid FROM pg_roles WHERE rolname=%s', (role,))
            target=cursor.fetchone()
            if target is None: raise ValueError('expected existing backend role')
            # MEMBER includes roles reachable by SET ROLE even when NOINHERIT
            # prevents their privileges from being active initially.
            cursor.execute('''SELECT oid,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolreplication
                              FROM pg_roles WHERE pg_has_role(%s,oid,'MEMBER')''',(target[0],))
            actors=cursor.fetchall()
            for actor,*privileged in actors:
                if any(privileged): raise ValueError('backend role has privileged membership')
                cursor.execute('''SELECT
                    has_schema_privilege(%s,'managed','CREATE'),
                    EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND datdba=%s),
                    EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                      WHERE n.nspname='managed' AND c.relkind IN ('r','p','v','m','f') AND
                      (has_table_privilege(%s,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR
                       has_any_column_privilege(%s,c.oid,'INSERT,UPDATE,REFERENCES'))),
                    EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                      WHERE n.nspname='managed' AND c.relkind='S' AND has_sequence_privilege(%s,c.oid,'USAGE,UPDATE')),
                    EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                      WHERE n.nspname='managed' AND p.oid<>'managed.apply_rows(uuid,uuid,uuid,jsonb,boolean)'::regprocedure
                      AND has_function_privilege(%s,p.oid,'EXECUTE'))''',(actor,actor,actor,actor,actor,actor))
                if any(cursor.fetchone()): raise ValueError('backend role has existing managed bypass privileges')
            cursor.execute(f'GRANT USAGE ON SCHEMA managed TO "{role}"')
            cursor.execute(f'GRANT EXECUTE ON FUNCTION managed.apply_rows(uuid,uuid,uuid,jsonb,boolean) TO "{role}"')


def export_identity(fid, object_id, feature_revision):
    if type(object_id) is not int or not 1 <= object_id <= 9223372036854775807: raise ValueError('invalid native ObjectID')
    return {'fid': identity(fid), 'object_id': str(object_id), 'feature_revision': identity(feature_revision)}
