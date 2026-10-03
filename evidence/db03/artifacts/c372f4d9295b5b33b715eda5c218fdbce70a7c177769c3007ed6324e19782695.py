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
                _require_unpinned_schema(cursor,group_id)
                cursor.execute('UPDATE managed.version SET head_revision=%s WHERE version_id=%s', (str(uuid.uuid4()),version_id))
                cursor.execute('UPDATE managed.version_group SET schema_generation=schema_generation+1 WHERE group_id=%s', (group_id,))
            if definition['geometry'] is not None:
                cursor.execute('SELECT 1 FROM public.spatial_ref_sys WHERE srid=%s', (definition['geometry']['srid'],))
                if cursor.fetchone() is None: raise ValueError('unknown retained spatial reference')
            cursor.execute('INSERT INTO managed.dataset VALUES(%s,%s,%s,%s,%s,%s)',
                           (dataset_id,managed_name,identity(catalog_item_id),identity(policy_ref),group_id,schema_id))
            text = canonical(definition)
            cursor.execute('INSERT INTO managed.schema_revision VALUES(%s,%s,%s,%s,%s::jsonb,%s)',
                           (schema_id,dataset_id,encoding(definition),text,text,fingerprint(definition)))
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
            install_constraints(cursor,dataset_id,group_id,definition)
            if definition['geometry'] is not None:
                cursor.execute(f'CREATE INDEX ON managed."{table(dataset_id)}" USING gist(geom)')
            cursor.execute(f'REVOKE ALL ON TABLE managed."{table(dataset_id)}" FROM PUBLIC')
            cursor.execute(f'REVOKE ALL ON SEQUENCE managed."{sequence}" FROM PUBLIC')
            _install_snapshots(cursor,dataset_id)
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
            'schema_sha256': row[7], 'canonical_encoding': encoding(definition),
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


def _grant_backend(connection, role, grants):
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
            cursor.execute('''SELECT oid,rolname,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolreplication
                              FROM pg_roles WHERE pg_has_role(%s,oid,'MEMBER')''',(target[0],))
            actors=cursor.fetchall()
            for actor,actor_name,*privileged in actors:
                # Server capability roles can bypass ordinary ACLs without any
                # of the rolsuper-style attributes (file/program execution etc).
                if any(privileged) or actor_name.startswith('pg_'):
                    raise ValueError('backend role has privileged membership')
                cursor.execute('''SELECT
                    has_schema_privilege(%s,'managed','CREATE'),
                    EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND datdba=%s),
                    EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='managed' AND nspowner=%s),
                    EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='managed' AND c.relowner=%s),
                    EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='managed' AND p.proowner=%s),
                    EXISTS(SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='managed' AND t.typowner=%s),
                    EXISTS(SELECT 1 FROM pg_extension WHERE extname='postgis' AND extowner=%s),
                    EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                      WHERE CASE WHEN n.nspname='managed' AND c.relkind IN ('r','p','v','m','f') THEN
                      (has_table_privilege(%s,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR
                       has_any_column_privilege(%s,c.oid,'INSERT,UPDATE,REFERENCES')) ELSE false END),
                    EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                      WHERE CASE WHEN n.nspname='managed' AND c.relkind='S' THEN has_sequence_privilege(%s,c.oid,'USAGE,UPDATE') ELSE false END),
                    EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                      WHERE n.nspname='managed' AND p.oid NOT IN (SELECT to_regprocedure(f) FROM unnest(%s::text[]) f WHERE to_regprocedure(f) IS NOT NULL)
                      AND has_function_privilege(%s,p.oid,'EXECUTE'))''',(actor,)*10+(list(SERVICE_FUNCTIONS+BRANCH_FUNCTIONS),actor))
                if any(cursor.fetchone()): raise ValueError('backend role has existing managed bypass privileges')
            cursor.execute(f'GRANT USAGE ON SCHEMA managed TO "{role}"')
            cursor.execute('GRANT EXECUTE ON FUNCTION '+', '.join(grants)+f' TO "{role}"')


def export_identity(fid, object_id, feature_revision):
    if type(object_id) is not int or not 1 <= object_id <= 9223372036854775807: raise ValueError('invalid native ObjectID')
    return {'fid': identity(fid), 'object_id': str(object_id), 'feature_revision': identity(feature_revision)}


def encoding(definition):
    return 'ambisgis-typed-schema-json-v'+str(definition['schema_version'])


def install_constraints(cursor, dataset_id, group_id, definition):
    """Migrator-only; validated finite CHECK/relationship constraints."""
    from .rules import checks
    prefix='db02_'+uuid.UUID(dataset_id).hex+'_'
    clauses=['CHECK ('+p+')' for p in checks(definition)]
    for relation in definition.get('relationships',[]):
        target=relation['target_dataset']
        cursor.execute('SELECT group_id FROM managed.dataset WHERE dataset_id=%s',(target,))
        row=cursor.fetchone()
        if row is None or str(row[0])!=group_id:raise ValueError('relationship target must be in same group')
        field='"'+relation['field']+'"'
        clauses.append('FOREIGN KEY(version_id,'+field+') REFERENCES managed."'+table(target)+'"(version_id,fid) DEFERRABLE INITIALLY IMMEDIATE')
        if relation['cardinality']=='one-to-one':
            clauses.append('UNIQUE(version_id,'+field+') DEFERRABLE INITIALLY IMMEDIATE')
    for index,clause in enumerate(clauses):
        cursor.execute('ALTER TABLE managed."'+table(dataset_id)+'" ADD CONSTRAINT "'+prefix+str(index)+'" '+clause)


def revise_schema(connection, descriptor, definition):
    """Change rules/defaults/domains/relations only, validating all existing rows."""
    require_idle(connection)
    definition=normalized(definition);dataset_id=identity(descriptor['dataset_id'])
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='30s'; SET LOCAL timezone='UTC'")
            cursor.execute('SELECT group_id FROM managed.dataset WHERE dataset_id=%s',(dataset_id,))
            found=cursor.fetchone()
            if found is None:raise ValueError('unknown dataset')
            group_id=str(found[0])
            cursor.execute('SELECT default_version_id FROM managed.version_group WHERE group_id=%s FOR UPDATE',(group_id,))
            version_id=str(cursor.fetchone()[0])
            cursor.execute('SELECT head_revision FROM managed.version WHERE version_id=%s FOR UPDATE',(version_id,))
            head=str(cursor.fetchone()[0])
            cursor.execute('SELECT d.active_schema_revision,r.definition FROM managed.dataset d JOIN managed.schema_revision r ON r.schema_revision_id=d.active_schema_revision WHERE d.dataset_id=%s',(dataset_id,))
            revision,old=cursor.fetchone()
            if str(revision)!=descriptor['schema_revision_id'] or head!=descriptor['current']['head_revision']:raise ValueError('STALE_SCHEMA')
            _require_unpinned_schema(cursor,group_id)
            def physical(value):
                return {'geometry':value['geometry'],'fields':[{k:v for k,v in f.items() if k not in ('default','domain')} for f in value['fields']]}
            if physical(old)!=physical(definition):raise ValueError('PHYSICAL_SCHEMA_CHANGE_REQUIRES_MIGRATION')
            if canonical(old)!=canonical(definition):
                prefix='db02_'+uuid.UUID(dataset_id).hex+'_'
                cursor.execute('SELECT conname FROM pg_constraint WHERE conrelid=%s::regclass',('managed.'+table(dataset_id),))
                for (name,) in cursor.fetchall():
                    if name.startswith(prefix):cursor.execute('ALTER TABLE managed."'+table(dataset_id)+'" DROP CONSTRAINT "'+name+'"')
                install_constraints(cursor,dataset_id,group_id,definition)
                revision_id=str(uuid.uuid4());text=canonical(definition)
                cursor.execute('INSERT INTO managed.schema_revision VALUES(%s,%s,%s,%s,%s::jsonb,%s)',
                               (revision_id,dataset_id,encoding(definition),text,text,fingerprint(definition)))
                cursor.execute('UPDATE managed.dataset SET active_schema_revision=%s WHERE dataset_id=%s',(revision_id,dataset_id))
                _install_snapshots(cursor,dataset_id)
                cursor.execute('UPDATE managed.version_group SET schema_generation=schema_generation+1 WHERE group_id=%s',(group_id,))
                cursor.execute('UPDATE managed.version SET head_revision=%s WHERE version_id=%s',(str(uuid.uuid4()),version_id))
    return export_schema(connection,dataset_id)


def apply_group(connection, descriptor, edits):
    require_idle(connection)
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='30s'; SET LOCAL timezone='UTC'")
            cursor.execute('SELECT managed.apply_group(%s,%s,%s::jsonb)',
                           (descriptor['current']['version_id'],descriptor['current']['head_revision'],
                            json.dumps(edits,ensure_ascii=False,separators=(',',':'),allow_nan=False)))
            return str(cursor.fetchone()[0])


SERVICE_FUNCTIONS = ('managed.apply_rows(uuid,uuid,uuid,jsonb,boolean)',
                     'managed.apply_group(uuid,uuid,jsonb)')
BRANCH_FUNCTIONS = (
    'managed.branch_get(uuid,boolean)', 'managed.branch_list(uuid)',
    'managed.branch_reserve(uuid,uuid,text,uuid,text,uuid[],uuid)',
    'managed.branch_populate(uuid,uuid)', 'managed.branch_fail(uuid,uuid,text)',
    'managed.branch_cancel(uuid,uuid)', 'managed.branch_recover(uuid,uuid)',
    'managed.branch_update(uuid,uuid,text,text,uuid[])',
    'managed.branch_transition(uuid,uuid,text)',
)


def grant_service(connection, role):
    """Grant DEFAULT edit primitives only, after the existing bypass checks."""
    _grant_backend(connection, role, SERVICE_FUNCTIONS)


def grant_branch_worker(connection, role):
    """Grant finite branch routines only; external policy checks remain required."""
    _grant_backend(connection, role, BRANCH_FUNCTIONS)


def _require_unpinned_schema(cursor, group_id):
    # The caller already owns the group FOR UPDATE guard. Failed reservations
    # do not pin a schema; retained sealed data does, even after logical delete.
    cursor.execute("SELECT to_regclass('managed.branch')")
    if cursor.fetchone()[0] is None:
        cursor.execute('SELECT count(*) FROM managed.version WHERE group_id=%s',(group_id,))
        blocked = cursor.fetchone()[0] != 1
    else:
        cursor.execute("""SELECT EXISTS(SELECT 1 FROM managed.branch WHERE group_id=%s
          AND state IN ('active','archived','deletion_pending'))
          OR EXISTS(SELECT 1 FROM managed.snapshot WHERE group_id=%s)""",(group_id,group_id))
        blocked = cursor.fetchone()[0]
    if blocked:
        raise ValueError('ACTIVE_BRANCH_SCHEMA_CHANGE_UNSUPPORTED')


def _install_snapshots(cursor, dataset_id):
    # Older installs are supported for explicit forward-migration tests.
    cursor.execute("SELECT to_regprocedure('managed.install_snapshot_table(uuid)')")
    if cursor.fetchone()[0] is not None:
        cursor.execute('SELECT managed.install_snapshot_table(%s)',(dataset_id,))
        cursor.execute('SELECT managed.install_snapshot_relations(%s)',(dataset_id,))
