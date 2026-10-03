-- SPDX-License-Identifier: GPL-3.0-or-later
-- DB-03: bounded typed snapshots. 001/002 remain checksum-locked.
-- All ordinary writes remain backend routines; this is not an end-user ACL.
-- Group schema writers must lock FOR UPDATE and advance schema_generation.
SELECT group_id FROM managed.version_group ORDER BY group_id FOR UPDATE;
ALTER TABLE managed.version DROP CONSTRAINT version_name_check;
ALTER TABLE managed.version ADD CONSTRAINT version_name_check
  CHECK(name='DEFAULT' OR name ~ '^[a-z][a-z0-9_]{0,62}$');
DROP TRIGGER immutable_version_identity ON managed.version;
CREATE TRIGGER immutable_version_identity BEFORE UPDATE OF version_id,group_id ON managed.version
  FOR EACH ROW EXECUTE FUNCTION managed.immutable();

CREATE TABLE managed.branch (
  branch_id uuid PRIMARY KEY,
  group_id uuid NOT NULL,
  owner_id uuid NOT NULL,
  visibility text NOT NULL CHECK(visibility IN ('private','shared','organization')),
  editor_policy_refs uuid[] NOT NULL,
  state text NOT NULL CHECK(state IN ('creating','active','archived','deletion_pending','deleted','creation_failed')),
  base_snapshot_id uuid,
  revision uuid NOT NULL,
  operation_id uuid NOT NULL,
  request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  touched_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  failure_code text CHECK(failure_code IN ('cancelled','recovered','copy_failed')),
  UNIQUE(group_id,branch_id), UNIQUE(group_id,operation_id),
  FOREIGN KEY(group_id,branch_id) REFERENCES managed.version(group_id,version_id)
);
CREATE TABLE managed.snapshot (
  snapshot_id uuid PRIMARY KEY,
  group_id uuid NOT NULL,
  branch_id uuid NOT NULL,
  source_version_id uuid NOT NULL,
  source_head uuid NOT NULL,
  schema_generation bigint NOT NULL CHECK(schema_generation>0),
  state text NOT NULL CHECK(state IN ('building','sealed')),
  created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  sealed_at timestamptz,
  FOREIGN KEY(group_id,branch_id) REFERENCES managed.branch(group_id,branch_id),
  FOREIGN KEY(group_id,source_version_id) REFERENCES managed.version(group_id,version_id),
  UNIQUE(group_id,snapshot_id), UNIQUE(branch_id,snapshot_id),
  CHECK((state='sealed')=(sealed_at IS NOT NULL))
);
ALTER TABLE managed.branch ADD CONSTRAINT branch_base_snapshot
  FOREIGN KEY(branch_id,base_snapshot_id) REFERENCES managed.snapshot(branch_id,snapshot_id)
  DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE managed.snapshot_dataset (
  snapshot_id uuid NOT NULL REFERENCES managed.snapshot,
  dataset_id uuid NOT NULL,
  schema_revision_id uuid NOT NULL,
  schema_sha256 text NOT NULL CHECK(schema_sha256 ~ '^[0-9a-f]{64}$'),
  row_count bigint NOT NULL CHECK(row_count>=0),
  row_bytes bigint NOT NULL CHECK(row_bytes>=0),
  rows_sha256 text NOT NULL CHECK(rows_sha256 ~ '^[0-9a-f]{64}$'),
  PRIMARY KEY(snapshot_id,dataset_id),
  FOREIGN KEY(dataset_id,schema_revision_id) REFERENCES managed.schema_revision(dataset_id,schema_revision_id)
);
CREATE TABLE managed.commit (
  commit_id uuid PRIMARY KEY,
  version_id uuid NOT NULL REFERENCES managed.version,
  parent_commit uuid REFERENCES managed.commit,
  snapshot_id uuid NOT NULL REFERENCES managed.snapshot,
  actor_id uuid NOT NULL,
  request_id uuid NOT NULL,
  schema_generation bigint NOT NULL CHECK(schema_generation>0),
  provenance text NOT NULL CHECK(provenance='branch_creation'),
  created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);
CREATE TRIGGER immutable_commit BEFORE UPDATE OR DELETE ON managed.commit
  FOR EACH ROW EXECUTE FUNCTION managed.immutable();
CREATE TABLE managed.branch_quota (
  group_id uuid PRIMARY KEY REFERENCES managed.version_group,
  limits jsonb NOT NULL,
  current_rows bigint NOT NULL DEFAULT 0 CHECK(current_rows>=0),
  snapshot_rows bigint NOT NULL DEFAULT 0 CHECK(snapshot_rows>=0),
  current_bytes bigint NOT NULL DEFAULT 0 CHECK(current_bytes>=0),
  snapshot_bytes bigint NOT NULL DEFAULT 0 CHECK(snapshot_bytes>=0),
  history_rows bigint NOT NULL DEFAULT 0 CHECK(history_rows>=0),
  attachment_bytes bigint NOT NULL DEFAULT 0 CHECK(attachment_bytes=0)
);
CREATE TABLE managed.snapshot_reference (
  snapshot_id uuid NOT NULL REFERENCES managed.snapshot,
  kind text NOT NULL CHECK(kind IN ('live_branch','history','publication','recovery')),
  reference_id uuid NOT NULL,
  PRIMARY KEY(snapshot_id,kind,reference_id)
);
REVOKE ALL ON ALL TABLES IN SCHEMA managed FROM PUBLIC;

CREATE FUNCTION managed.branch_version_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,managed,pg_temp AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM managed.version_group WHERE default_version_id=NEW.version_id) THEN
    IF NEW.name<>'DEFAULT' THEN RAISE EXCEPTION 'DEFAULT_IDENTITY_IMMUTABLE'; END IF;
  ELSIF NEW.name='DEFAULT' OR NOT EXISTS(SELECT 1 FROM managed.branch WHERE branch_id=NEW.version_id AND group_id=NEW.group_id) THEN
    RAISE EXCEPTION 'NAMED_VERSION_REQUIRES_BRANCH_RESERVATION';
  END IF;
  RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER managed_branch_version AFTER INSERT OR UPDATE ON managed.version
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION managed.branch_version_guard();

CREATE FUNCTION managed.branch_get(p_branch uuid,p_operational boolean DEFAULT false)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp AS $$
 SELECT (to_jsonb(b)-'request_sha256')||jsonb_build_object('name',v.name,'head_revision',v.head_revision,
         'authorization','backend policy check required; references do not grant access')
 FROM managed.branch b JOIN managed.version v ON v.version_id=b.branch_id
 WHERE b.branch_id=p_branch AND (p_operational OR b.state IN ('active','archived'))
$$;
CREATE FUNCTION managed.branch_list(p_group uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp AS $$
 SELECT coalesce(jsonb_agg(managed.branch_get(branch_id,false) ORDER BY branch_id),'[]'::jsonb)
 FROM managed.branch WHERE group_id=p_group AND state IN ('active','archived')
$$;

CREATE FUNCTION managed.configure_branch_quotas(p_group uuid,p_limits jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp AS $$
DECLARE key text; n numeric; q managed.branch_quota; used_count bigint;
 keys text[]:=ARRAY['max_branches','max_current_rows','max_snapshot_rows','max_current_bytes','max_snapshot_bytes',
 'max_source_rows','max_source_bytes','max_history_rows','max_attachment_bytes','max_idle_seconds'];
BEGIN
 IF p_limits IS NULL OR jsonb_typeof(p_limits)<>'object'
    OR (SELECT count(*) FROM jsonb_object_keys(p_limits))<>cardinality(keys)
    OR NOT p_limits ?& keys THEN RAISE EXCEPTION 'INVALID_BRANCH_QUOTAS'; END IF;
 FOREACH key IN ARRAY keys LOOP
   IF jsonb_typeof(p_limits->key)<>'number' OR (p_limits->>key)!~'^[0-9]+$' THEN RAISE EXCEPTION 'INVALID_BRANCH_QUOTAS'; END IF;
   n:=(p_limits->>key)::numeric;
   IF n>1073741824 OR (key<>'max_attachment_bytes' AND n<1) OR
      (key='max_attachment_bytes' AND n<>0) OR
      (key='max_branches' AND n>100) OR
      (key IN ('max_current_rows','max_snapshot_rows','max_source_rows','max_history_rows') AND n>1000000) OR
      (key='max_idle_seconds' AND n>31536000) THEN RAISE EXCEPTION 'INVALID_BRANCH_QUOTAS'; END IF;
 END LOOP;
 PERFORM 1 FROM managed.version_group WHERE group_id=p_group FOR KEY SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'UNKNOWN_VERSION_GROUP'; END IF;
 INSERT INTO managed.branch_quota(group_id,limits) VALUES(p_group,p_limits) ON CONFLICT DO NOTHING;
 SELECT * INTO q FROM managed.branch_quota WHERE group_id=p_group FOR UPDATE;
 SELECT count(*) INTO used_count FROM managed.branch WHERE group_id=p_group AND state IN ('creating','active','archived','deletion_pending');
 IF used_count>(p_limits->>'max_branches')::bigint OR q.current_rows>(p_limits->>'max_current_rows')::bigint
 OR q.snapshot_rows>(p_limits->>'max_snapshot_rows')::bigint OR q.current_bytes>(p_limits->>'max_current_bytes')::bigint
 OR q.snapshot_bytes>(p_limits->>'max_snapshot_bytes')::bigint OR q.history_rows>(p_limits->>'max_history_rows')::bigint THEN
   RAISE EXCEPTION 'QUOTA_BELOW_RETAINED_USAGE';
 END IF;
 UPDATE managed.branch_quota SET limits=p_limits WHERE group_id=p_group;
END $$;

CREATE FUNCTION managed.branch_reserve(p_group uuid,p_branch uuid,p_name text,p_owner uuid,
 p_visibility text,p_editors uuid[],p_operation uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp AS $$
DECLARE request_hash text; prior managed.branch; q managed.branch_quota; editors uuid[]; n bigint;
BEGIN
 IF p_group IS NULL OR p_branch IS NULL OR p_owner IS NULL OR p_operation IS NULL
 OR p_name IS NULL OR p_name!~'^[a-z][a-z0-9_]{0,62}$' OR p_visibility IS NULL
 OR p_visibility NOT IN ('private','shared','organization') OR p_editors IS NULL
 OR cardinality(p_editors)>64 OR array_position(p_editors,NULL) IS NOT NULL THEN RAISE EXCEPTION 'INVALID_BRANCH_REQUEST'; END IF;
 SELECT coalesce(array_agg(DISTINCT value ORDER BY value),ARRAY[]::uuid[]) INTO editors FROM unnest(p_editors) value;
 IF (p_visibility='private' AND cardinality(editors)<>0) OR (p_visibility='shared' AND cardinality(editors)=0) THEN
   RAISE EXCEPTION 'INVALID_BRANCH_VISIBILITY';
 END IF;
 request_hash:=encode(sha256(convert_to(jsonb_build_array(p_group,p_branch,p_name,p_owner,p_visibility,editors)::text,'UTF8')),'hex');
 PERFORM 1 FROM managed.version_group WHERE group_id=p_group FOR KEY SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'UNKNOWN_VERSION_GROUP'; END IF;
 SELECT * INTO q FROM managed.branch_quota WHERE group_id=p_group FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'BRANCH_QUOTAS_REQUIRED'; END IF;
 SELECT * INTO prior FROM managed.branch WHERE group_id=p_group AND operation_id=p_operation;
 IF FOUND THEN
   IF prior.request_sha256<>request_hash THEN RAISE EXCEPTION 'BRANCH_OPERATION_CONFLICT'; END IF;
   RETURN managed.branch_get(prior.branch_id,true);
 END IF;
 SELECT count(*) INTO n FROM managed.branch WHERE group_id=p_group AND state IN ('creating','active','archived','deletion_pending');
 IF n >= (q.limits->>'max_branches')::bigint THEN RAISE EXCEPTION 'BRANCH_COUNT_QUOTA'; END IF;
 INSERT INTO managed.version(version_id,group_id,name,head_revision) VALUES(p_branch,p_group,p_name,gen_random_uuid());
 INSERT INTO managed.branch(branch_id,group_id,owner_id,visibility,editor_policy_refs,state,revision,operation_id,request_sha256)
 VALUES(p_branch,p_group,p_owner,p_visibility,editors,'creating',gen_random_uuid(),p_operation,request_hash);
 RETURN managed.branch_get(p_branch,true);
END $$;

CREATE FUNCTION managed.snapshot_rows_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,managed,pg_temp AS $$
BEGIN
 IF TG_OP<>'INSERT' OR NOT EXISTS(SELECT 1 FROM managed.snapshot WHERE snapshot_id=NEW.snapshot_id AND state='building') THEN
   RAISE EXCEPTION 'IMMUTABLE_SEALED_SNAPSHOT';
 END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION managed.snapshot_manifest_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,managed,pg_temp AS $$
BEGIN
 IF TG_OP<>'INSERT' OR NOT EXISTS(SELECT 1 FROM managed.snapshot WHERE snapshot_id=NEW.snapshot_id AND state='building') THEN
   RAISE EXCEPTION 'IMMUTABLE_SNAPSHOT_MANIFEST';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER snapshot_manifest_immutable BEFORE INSERT OR UPDATE OR DELETE ON managed.snapshot_dataset
 FOR EACH ROW EXECUTE FUNCTION managed.snapshot_manifest_guard();
CREATE FUNCTION managed.snapshot_seal_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,managed,pg_temp AS $$
BEGIN
 IF TG_OP<>'UPDATE' OR OLD.state<>'building' OR NEW.state<>'sealed'
 OR (to_jsonb(OLD)-'state'-'sealed_at')<>(to_jsonb(NEW)-'state'-'sealed_at') THEN
   RAISE EXCEPTION 'IMMUTABLE_SEALED_SNAPSHOT';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER snapshot_immutable BEFORE UPDATE OR DELETE ON managed.snapshot
 FOR EACH ROW EXECUTE FUNCTION managed.snapshot_seal_guard();

-- Called only by the schema owner under the group schema guard.
CREATE FUNCTION managed.install_snapshot_table(p_dataset uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog,managed,pg_temp AS $$
DECLARE d managed.dataset; t text; cols text; item record; definition jsonb; relation jsonb; i integer:=0;
BEGIN
 SELECT * INTO d FROM managed.dataset WHERE dataset_id=p_dataset;
 IF NOT FOUND THEN RAISE EXCEPTION 'UNKNOWN_DATASET'; END IF;
 PERFORM 1 FROM managed.version_group WHERE group_id=d.group_id FOR UPDATE;
 t:='s_'||replace(p_dataset::text,'-','');
 IF to_regclass('managed.'||t) IS NULL THEN
   SELECT string_agg(format('%I %s%s',attname,format_type(atttypid,atttypmod),CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END),',' ORDER BY attnum)
   INTO cols FROM pg_attribute WHERE attrelid=('managed.d_'||replace(p_dataset::text,'-',''))::regclass
      AND attnum>0 AND NOT attisdropped AND attname<>'version_id';
   EXECUTE format('CREATE TABLE managed.%I (snapshot_id uuid NOT NULL REFERENCES managed.snapshot,%s,PRIMARY KEY(snapshot_id,fid),UNIQUE(snapshot_id,object_id),FOREIGN KEY(dataset_id,fid,object_id) REFERENCES managed.feature_identity(dataset_id,fid,object_id))',t,cols);
   EXECUTE format('CREATE TRIGGER snapshot_rows_immutable BEFORE INSERT OR UPDATE OR DELETE ON managed.%I FOR EACH ROW EXECUTE FUNCTION managed.snapshot_rows_guard()',t);
   EXECUTE format('REVOKE ALL ON TABLE managed.%I FROM PUBLIC',t);
   IF EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=('managed.'||t)::regclass AND attname='geom' AND NOT attisdropped) THEN
     EXECUTE format('CREATE INDEX ON managed.%I USING gist(geom)',t);
   END IF;
 END IF;
END $$;
CREATE FUNCTION managed.install_snapshot_relations(p_dataset uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog,managed,pg_temp AS $$
DECLARE d managed.dataset; definition jsonb; relation jsonb; t text; old record; i integer:=0;
BEGIN
 SELECT * INTO d FROM managed.dataset WHERE dataset_id=p_dataset;
 PERFORM 1 FROM managed.version_group WHERE group_id=d.group_id FOR UPDATE;
 SELECT r.definition INTO definition FROM managed.schema_revision r WHERE schema_revision_id=d.active_schema_revision;
 t:='s_'||replace(p_dataset::text,'-','');
 FOR old IN SELECT conname FROM pg_constraint WHERE conrelid=('managed.'||t)::regclass
   AND position(('db03_'||replace(p_dataset::text,'-','')||'_') IN conname::text)=1 LOOP
   EXECUTE format('ALTER TABLE managed.%I DROP CONSTRAINT %I',t,old.conname);
 END LOOP;
 FOR relation IN SELECT value FROM jsonb_array_elements(coalesce(definition->'relationships','[]')) LOOP
   EXECUTE format('ALTER TABLE managed.%I ADD CONSTRAINT %I FOREIGN KEY(snapshot_id,%I) REFERENCES managed.%I(snapshot_id,fid) DEFERRABLE INITIALLY IMMEDIATE',
     t,'db03_'||replace(p_dataset::text,'-','')||'_'||i,relation->>'field','s_'||replace(relation->>'target_dataset','-',''));
   i:=i+1;
   IF relation->>'cardinality'='one-to-one' THEN
     EXECUTE format('ALTER TABLE managed.%I ADD CONSTRAINT %I UNIQUE(snapshot_id,%I) DEFERRABLE INITIALLY IMMEDIATE',
       t,'db03_'||replace(p_dataset::text,'-','')||'_'||i,relation->>'field');
     i:=i+1;
   END IF;
 END LOOP;
END $$;
DO $$ DECLARE d record; BEGIN
 FOR d IN SELECT dataset_id FROM managed.dataset ORDER BY dataset_id LOOP PERFORM managed.install_snapshot_table(d.dataset_id); END LOOP;
 FOR d IN SELECT dataset_id FROM managed.dataset ORDER BY dataset_id LOOP PERFORM managed.install_snapshot_relations(d.dataset_id); END LOOP;
END $$;

CREATE FUNCTION managed.branch_constraints(p_group uuid,p_defer boolean) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog,managed,pg_temp AS $$
DECLARE c record;
BEGIN
 FOR c IN SELECT co.conname FROM pg_constraint co JOIN pg_class t ON t.oid=co.conrelid
 JOIN pg_namespace n ON n.oid=t.relnamespace JOIN managed.dataset d
 ON t.relname IN ('d_'||replace(d.dataset_id::text,'-',''),'s_'||replace(d.dataset_id::text,'-',''))
 WHERE n.nspname='managed' AND d.group_id=p_group AND co.condeferrable
 AND (position(('db02_'||replace(d.dataset_id::text,'-','')||'_') IN co.conname::text)=1
   OR position(('db03_'||replace(d.dataset_id::text,'-','')||'_') IN co.conname::text)=1)
 ORDER BY co.conname LOOP
  EXECUTE format('SET CONSTRAINTS managed.%I %s',c.conname,CASE WHEN p_defer THEN 'DEFERRED' ELSE 'IMMEDIATE' END);
 END LOOP;
END $$;

-- Per-row canonical JSONB contains typed scalars; geometry is exact XDR EWKB.
-- The schema hash is part of the dataset digest. Hash aggregation is bounded
-- by source row/byte limits, and aggregates fixed-size row digests only.
CREATE FUNCTION managed.snapshot_measure(p_dataset uuid,p_identity uuid,p_snapshot boolean,p_schema_hash text)
RETURNS TABLE(row_count bigint,row_bytes bigint,rows_sha256 text)
LANGUAGE plpgsql SET search_path=pg_catalog,managed,pg_temp AS $$
DECLARE t text; predicate text; payload text;
BEGIN
 t:=CASE WHEN p_snapshot THEN 's_' ELSE 'd_' END||replace(p_dataset::text,'-','');
 predicate:=CASE WHEN p_snapshot THEN 'snapshot_id' ELSE 'version_id' END;
 payload:='to_jsonb(t)-''version_id''-''snapshot_id''-''geom''';
 IF EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=('managed.'||t)::regclass AND attname='geom' AND NOT attisdropped) THEN
  payload:=payload||'||jsonb_build_object(''geom'',encode(public.ST_AsEWKB(t.geom,''XDR''),''hex''))';
 END IF;
 RETURN QUERY EXECUTE format(
  'SELECT count(*),coalesce(sum(pg_column_size(t)),0)::bigint,encode(sha256(convert_to($2||'':''||coalesce(string_agg(encode(sha256(convert_to((%s)::text,''UTF8'')),''hex''),'''' ORDER BY fid),''''),''UTF8'')),''hex'') FROM managed.%I t WHERE %I=$1',
  payload,t,predicate) USING p_identity,p_schema_hash;
END $$;

CREATE FUNCTION managed.branch_populate(p_branch uuid,p_expected uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp
 SET timezone='UTC' SET datestyle='ISO,YMD' AS $$
DECLARE g managed.version_group; b managed.branch; q managed.branch_quota; d record;
 source_head uuid; snapshot_value uuid:=gen_random_uuid(); commit_value uuid:=gen_random_uuid();
 cols text; select_cols text; before_state record; snapshot_state record; branch_state record;
 source_rows bigint:=0; source_bytes bigint:=0; copied_rows bigint:=0; copied_current_bytes bigint:=0;
 copied_snapshot_bytes bigint:=0; manifests jsonb:='{}'::jsonb; entry jsonb;
BEGIN
 IF current_setting('transaction_isolation')<>'repeatable read' THEN RAISE EXCEPTION 'SNAPSHOT_REQUIRES_REPEATABLE_READ'; END IF;
 IF p_branch IS NULL OR p_expected IS NULL THEN RAISE EXCEPTION 'INVALID_BRANCH_REQUEST'; END IF;
 -- First data read and first row lock. DDL must UPDATE this row. A DDL commit
 -- after this RR snapshot but before this lock therefore raises 40001.
 SELECT vg.* INTO g FROM managed.version_group vg JOIN managed.branch br ON br.group_id=vg.group_id
 WHERE br.branch_id=p_branch FOR SHARE OF vg;
 IF NOT FOUND THEN RAISE EXCEPTION 'UNKNOWN_BRANCH'; END IF;
 SELECT * INTO q FROM managed.branch_quota WHERE group_id=g.group_id FOR UPDATE;
 PERFORM 1 FROM managed.version WHERE version_id=p_branch FOR UPDATE;
 SELECT * INTO b FROM managed.branch WHERE branch_id=p_branch FOR UPDATE;
 IF b.revision<>p_expected THEN RAISE EXCEPTION 'STALE_BRANCH_METADATA'; END IF;
 IF b.state<>'creating' THEN RAISE EXCEPTION 'BRANCH_NOT_CREATING'; END IF;
 IF statement_timestamp()-b.created_at>make_interval(secs=>(q.limits->>'max_idle_seconds')::integer) THEN
  RAISE EXCEPTION 'BRANCH_IDLE_EXPIRED';
 END IF;
 SELECT head_revision INTO source_head FROM managed.version WHERE version_id=g.default_version_id;
 FOR d IN SELECT ds.*,r.schema_sha256 FROM managed.dataset ds JOIN managed.schema_revision r
 ON r.schema_revision_id=ds.active_schema_revision WHERE ds.group_id=g.group_id ORDER BY ds.dataset_id LOOP
  -- Preflight before hashing or copying; DEFAULT never takes the quota lock.
  EXECUTE format('SELECT count(*) AS row_count,coalesce(sum(pg_column_size(t)),0)::bigint AS row_bytes FROM managed.%I t WHERE version_id=$1',
    'd_'||replace(d.dataset_id::text,'-','')) INTO before_state USING g.default_version_id;
  source_rows:=source_rows+before_state.row_count; source_bytes:=source_bytes+before_state.row_bytes;
  IF source_rows>(q.limits->>'max_source_rows')::bigint OR source_bytes>(q.limits->>'max_source_bytes')::bigint THEN
   RAISE EXCEPTION 'BRANCH_SOURCE_BOUND';
  END IF;
 END LOOP;
 IF q.current_rows+source_rows>(q.limits->>'max_current_rows')::bigint
 OR q.snapshot_rows+source_rows>(q.limits->>'max_snapshot_rows')::bigint
 OR q.current_bytes+source_bytes>(q.limits->>'max_current_bytes')::bigint
 OR q.snapshot_bytes+source_bytes>(q.limits->>'max_snapshot_bytes')::bigint
 OR q.history_rows+1>(q.limits->>'max_history_rows')::bigint THEN RAISE EXCEPTION 'BRANCH_STORAGE_QUOTA'; END IF;
 INSERT INTO managed.snapshot(snapshot_id,group_id,branch_id,source_version_id,source_head,schema_generation,state)
 VALUES(snapshot_value,g.group_id,p_branch,g.default_version_id,source_head,g.schema_generation,'building');
 PERFORM managed.branch_constraints(g.group_id,true);
 FOR d IN SELECT ds.*,r.schema_sha256 FROM managed.dataset ds JOIN managed.schema_revision r
 ON r.schema_revision_id=ds.active_schema_revision WHERE ds.group_id=g.group_id ORDER BY ds.dataset_id LOOP
  SELECT * INTO before_state FROM managed.snapshot_measure(d.dataset_id,g.default_version_id,false,d.schema_sha256);
  SELECT string_agg(quote_ident(attname),',' ORDER BY attnum) INTO cols FROM pg_attribute
    WHERE attrelid=('managed.d_'||replace(d.dataset_id::text,'-',''))::regclass AND attnum>0
    AND NOT attisdropped AND attname<>'version_id';
  EXECUTE format('INSERT INTO managed.%I(snapshot_id,%s) SELECT $1,%s FROM managed.%I WHERE version_id=$2',
    's_'||replace(d.dataset_id::text,'-',''),cols,cols,'d_'||replace(d.dataset_id::text,'-',''))
    USING snapshot_value,g.default_version_id;
  EXECUTE format('INSERT INTO managed.%I(version_id,%s) SELECT $1,%s FROM managed.%I WHERE version_id=$2',
    'd_'||replace(d.dataset_id::text,'-',''),cols,cols,'d_'||replace(d.dataset_id::text,'-',''))
    USING p_branch,g.default_version_id;
  SELECT * INTO snapshot_state FROM managed.snapshot_measure(d.dataset_id,snapshot_value,true,d.schema_sha256);
  SELECT * INTO branch_state FROM managed.snapshot_measure(d.dataset_id,p_branch,false,d.schema_sha256);
  IF before_state.row_count<>snapshot_state.row_count OR before_state.rows_sha256<>snapshot_state.rows_sha256
  OR before_state.row_count<>branch_state.row_count OR before_state.rows_sha256<>branch_state.rows_sha256 THEN
   RAISE EXCEPTION 'SNAPSHOT_COPY_INTEGRITY';
  END IF;
  INSERT INTO managed.snapshot_dataset(snapshot_id,dataset_id,schema_revision_id,schema_sha256,row_count,row_bytes,rows_sha256)
    VALUES(snapshot_value,d.dataset_id,d.active_schema_revision,d.schema_sha256,snapshot_state.row_count,snapshot_state.row_bytes,snapshot_state.rows_sha256);
  copied_rows:=copied_rows+branch_state.row_count;
  copied_current_bytes:=copied_current_bytes+branch_state.row_bytes;
  copied_snapshot_bytes:=copied_snapshot_bytes+snapshot_state.row_bytes;
 END LOOP;
 PERFORM managed.branch_constraints(g.group_id,false);
 IF copied_rows<>source_rows OR q.current_bytes+copied_current_bytes>(q.limits->>'max_current_bytes')::bigint
 OR q.snapshot_bytes+copied_snapshot_bytes>(q.limits->>'max_snapshot_bytes')::bigint THEN RAISE EXCEPTION 'BRANCH_STORAGE_QUOTA'; END IF;
 UPDATE managed.snapshot SET state='sealed',sealed_at=transaction_timestamp() WHERE snapshot_id=snapshot_value;
 INSERT INTO managed.commit(commit_id,version_id,snapshot_id,actor_id,request_id,schema_generation,provenance)
 VALUES(commit_value,p_branch,snapshot_value,b.owner_id,b.operation_id,g.schema_generation,'branch_creation');
 INSERT INTO managed.snapshot_reference VALUES(snapshot_value,'live_branch',p_branch);
 UPDATE managed.version SET head_revision=commit_value WHERE version_id=p_branch;
 UPDATE managed.branch SET state='active',base_snapshot_id=snapshot_value,revision=gen_random_uuid(),
   touched_at=transaction_timestamp() WHERE branch_id=p_branch;
 UPDATE managed.branch_quota SET current_rows=current_rows+copied_rows,snapshot_rows=snapshot_rows+copied_rows,
   current_bytes=current_bytes+copied_current_bytes,snapshot_bytes=snapshot_bytes+copied_snapshot_bytes,
   history_rows=history_rows+1 WHERE group_id=g.group_id;
 RETURN managed.branch_get(p_branch,false);
END $$;

CREATE FUNCTION managed.branch_fail(p_branch uuid,p_expected uuid,p_reason text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp AS $$
DECLARE group_value uuid; b managed.branch;
BEGIN
 IF p_reason IS NULL OR p_reason NOT IN ('cancelled','recovered','copy_failed') THEN RAISE EXCEPTION 'INVALID_BRANCH_FAILURE'; END IF;
 SELECT group_id INTO group_value FROM managed.branch WHERE branch_id=p_branch;
 IF NOT FOUND THEN RAISE EXCEPTION 'UNKNOWN_BRANCH'; END IF;
 PERFORM 1 FROM managed.version_group WHERE group_id=group_value FOR KEY SHARE;
 PERFORM 1 FROM managed.branch_quota WHERE group_id=group_value FOR UPDATE;
 PERFORM 1 FROM managed.version WHERE version_id=p_branch FOR UPDATE;
 SELECT * INTO b FROM managed.branch WHERE branch_id=p_branch FOR UPDATE;
 IF p_expected IS NULL OR b.revision<>p_expected THEN RAISE EXCEPTION 'STALE_BRANCH_METADATA'; END IF;
 IF b.state<>'creating' THEN RAISE EXCEPTION 'BRANCH_NOT_CREATING'; END IF;
 IF EXISTS(SELECT 1 FROM managed.snapshot WHERE branch_id=p_branch) THEN RAISE EXCEPTION 'UNEXPECTED_COMMITTED_STAGING'; END IF;
 UPDATE managed.branch SET state='creation_failed',revision=gen_random_uuid(),failure_code=p_reason,
 touched_at=transaction_timestamp() WHERE branch_id=p_branch;
 RETURN managed.branch_get(p_branch,true);
END $$;
CREATE FUNCTION managed.branch_cancel(p_branch uuid,p_expected uuid) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp AS $$
 SELECT managed.branch_fail(p_branch,p_expected,'cancelled')
$$;
CREATE FUNCTION managed.branch_recover(p_branch uuid,p_expected uuid) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp AS $$
 SELECT managed.branch_fail(p_branch,p_expected,'recovered')
$$;

CREATE FUNCTION managed.branch_update(p_branch uuid,p_expected uuid,p_name text,p_visibility text,p_editors uuid[])
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp AS $$
DECLARE group_value uuid; b managed.branch; editors uuid[];
BEGIN
 IF p_name IS NULL OR p_name!~'^[a-z][a-z0-9_]{0,62}$' OR p_visibility IS NULL
 OR p_visibility NOT IN ('private','shared','organization') OR p_editors IS NULL
 OR cardinality(p_editors)>64 OR array_position(p_editors,NULL) IS NOT NULL THEN RAISE EXCEPTION 'INVALID_BRANCH_REQUEST'; END IF;
 SELECT coalesce(array_agg(DISTINCT value ORDER BY value),ARRAY[]::uuid[]) INTO editors FROM unnest(p_editors) value;
 IF (p_visibility='private' AND cardinality(editors)<>0) OR (p_visibility='shared' AND cardinality(editors)=0) THEN RAISE EXCEPTION 'INVALID_BRANCH_VISIBILITY'; END IF;
 SELECT group_id INTO group_value FROM managed.branch WHERE branch_id=p_branch;
 IF NOT FOUND THEN RAISE EXCEPTION 'UNKNOWN_BRANCH'; END IF;
 PERFORM 1 FROM managed.version_group WHERE group_id=group_value FOR KEY SHARE;
 PERFORM 1 FROM managed.branch_quota WHERE group_id=group_value FOR UPDATE;
 PERFORM 1 FROM managed.version WHERE version_id=p_branch FOR UPDATE;
 SELECT * INTO b FROM managed.branch WHERE branch_id=p_branch FOR UPDATE;
 IF p_expected IS NULL OR b.revision<>p_expected THEN RAISE EXCEPTION 'STALE_BRANCH_METADATA'; END IF;
 IF b.state<>'active' THEN RAISE EXCEPTION 'BRANCH_NOT_ACTIVE'; END IF;
 UPDATE managed.version SET name=p_name WHERE version_id=p_branch;
 UPDATE managed.branch SET visibility=p_visibility,editor_policy_refs=editors,revision=gen_random_uuid(),
 touched_at=transaction_timestamp() WHERE branch_id=p_branch;
 RETURN managed.branch_get(p_branch,false);
END $$;

CREATE FUNCTION managed.branch_transition(p_branch uuid,p_expected uuid,p_state text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp AS $$
DECLARE group_value uuid; b managed.branch;
BEGIN
 SELECT group_id INTO group_value FROM managed.branch WHERE branch_id=p_branch;
 IF NOT FOUND THEN RAISE EXCEPTION 'UNKNOWN_BRANCH'; END IF;
 PERFORM 1 FROM managed.version_group WHERE group_id=group_value FOR KEY SHARE;
 PERFORM 1 FROM managed.branch_quota WHERE group_id=group_value FOR UPDATE;
 PERFORM 1 FROM managed.version WHERE version_id=p_branch FOR UPDATE;
 SELECT * INTO b FROM managed.branch WHERE branch_id=p_branch FOR UPDATE;
 IF p_expected IS NULL OR b.revision<>p_expected THEN RAISE EXCEPTION 'STALE_BRANCH_METADATA'; END IF;
 IF p_state IS NULL OR NOT ((b.state='active' AND p_state='archived')
 OR (b.state IN ('active','archived') AND p_state='deletion_pending')
 OR (b.state='deletion_pending' AND p_state='deleted')) THEN RAISE EXCEPTION 'INVALID_BRANCH_TRANSITION'; END IF;
 IF p_state IN ('deletion_pending','deleted') AND EXISTS(SELECT 1 FROM managed.snapshot_reference
 WHERE snapshot_id=b.base_snapshot_id AND kind<>'live_branch') THEN RAISE EXCEPTION 'RETAINED_SNAPSHOT_REFERENCE'; END IF;
 UPDATE managed.branch SET state=p_state,revision=gen_random_uuid(),touched_at=transaction_timestamp() WHERE branch_id=p_branch;
 IF p_state='deleted' THEN DELETE FROM managed.snapshot_reference WHERE snapshot_id=b.base_snapshot_id AND kind='live_branch' AND reference_id=p_branch; END IF;
 -- Logical deletion retains both data copies and their quota accounting.
 RETURN managed.branch_get(p_branch,true);
END $$;

CREATE OR REPLACE FUNCTION managed.apply_group(p_version uuid,p_expected uuid,p_edits jsonb)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp AS $$
DECLARE
  group_value uuid; current_head uuid; edit jsonb; ds record; row_value jsonb;
  field jsonb; values_obj jsonb; prepared jsonb; variants jsonb; variant jsonb;
  selector text; dataset_value uuid; existing boolean; seen uuid[]:=ARRAY[]::uuid[];
  total integer:=0; keys text[]; constraint_row record;
BEGIN
  IF p_version IS NULL OR p_expected IS NULL OR p_edits IS NULL OR jsonb_typeof(p_edits)<>'array'
     OR jsonb_array_length(p_edits) NOT BETWEEN 1 AND 128 THEN RAISE EXCEPTION 'INVALID_MANAGED_GROUP'; END IF;
  SELECT group_id INTO group_value FROM managed.version WHERE version_id=p_version;
  IF NOT FOUND THEN RAISE EXCEPTION 'DATASET_VERSION_MISMATCH'; END IF;
  -- Data writers share the schema guard with branch creation; schema writers use UPDATE.
  PERFORM 1 FROM managed.version_group WHERE group_id=group_value FOR KEY SHARE;
  SELECT head_revision INTO current_head FROM managed.version WHERE version_id=p_version FOR UPDATE;
  IF current_head IS DISTINCT FROM p_expected THEN RAISE EXCEPTION 'STALE_HEAD'; END IF;
  FOR constraint_row IN SELECT c.conname FROM pg_constraint c
    JOIN pg_class t ON t.oid=c.conrelid
    JOIN pg_namespace n ON n.oid=t.relnamespace
    JOIN managed.dataset d ON t.relname='d_'||replace(d.dataset_id::text,'-','')
    WHERE n.nspname='managed' AND d.group_id=group_value AND c.condeferrable
      AND position(('db02_'||replace(d.dataset_id::text,'-','')||'_') IN c.conname::text)=1 LOOP
    EXECUTE format('SET CONSTRAINTS managed.%I DEFERRED',constraint_row.conname);
  END LOOP;
  FOR edit IN SELECT value FROM jsonb_array_elements(p_edits) LOOP
    IF jsonb_typeof(edit)<>'object' OR NOT edit ?& ARRAY['dataset_id','rows','replace']
       OR (SELECT count(*) FROM jsonb_object_keys(edit))<>3
       OR jsonb_typeof(edit->'dataset_id')<>'string' OR jsonb_typeof(edit->'rows')<>'array'
       OR jsonb_typeof(edit->'replace')<>'boolean' THEN RAISE EXCEPTION 'INVALID_MANAGED_GROUP_EDIT'; END IF;
    dataset_value:=(edit->>'dataset_id')::uuid;
    IF dataset_value=ANY(seen) THEN RAISE EXCEPTION 'DUPLICATE_DATASET_EDIT'; END IF;
    seen:=array_append(seen,dataset_value);total:=total+jsonb_array_length(edit->'rows');
    IF total>10000 THEN RAISE EXCEPTION 'MANAGED_BATCH_LIMIT'; END IF;
    SELECT d.*,r.definition INTO ds FROM managed.dataset d
      JOIN managed.schema_revision r ON r.schema_revision_id=d.active_schema_revision WHERE d.dataset_id=dataset_value;
    IF NOT FOUND OR ds.group_id<>group_value THEN RAISE EXCEPTION 'DATASET_VERSION_MISMATCH'; END IF;
    prepared:='[]'::jsonb;
    FOR row_value IN SELECT value FROM jsonb_array_elements(edit->'rows') LOOP
      IF jsonb_typeof(row_value)<>'object' OR jsonb_typeof(row_value->'fid') IS DISTINCT FROM 'string'
         OR jsonb_typeof(row_value->'values') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'INVALID_FEATURE_ROW'; END IF;
      values_obj:=row_value->'values';
      IF ds.definition->>'schema_version'='2' THEN
        EXECUTE format('SELECT EXISTS(SELECT 1 FROM managed.%I WHERE version_id=$1 AND fid=$2)',
           'd_'||replace(dataset_value::text,'-','')) INTO existing USING p_version,(row_value->>'fid')::uuid;
        -- Only inserts receive defaults. Full-row updates preserve DB01 semantics.
        IF NOT existing THEN
          selector:=ds.definition->'subtypes'->>'field';
          IF selector IS NOT NULL AND NOT values_obj ? selector THEN
            SELECT value INTO field FROM jsonb_array_elements(ds.definition->'fields') WHERE value->>'name'=selector;
            IF field ? 'default' THEN values_obj:=values_obj||jsonb_build_object(selector,field->'default'); END IF;
          END IF;
          variant:=NULL;
          IF selector IS NOT NULL THEN
            SELECT value INTO variant FROM jsonb_array_elements(ds.definition->'subtypes'->'variants')
              WHERE value->'code'=values_obj->selector;
            IF NOT FOUND THEN RAISE EXCEPTION 'INVALID_SUBTYPE'; END IF;
          END IF;
          FOR field IN SELECT value FROM jsonb_array_elements(ds.definition->'fields') LOOP
            IF NOT values_obj ? (field->>'name') THEN
              IF variant->'defaults' ? (field->>'name') THEN
                values_obj:=values_obj||jsonb_build_object(field->>'name',variant->'defaults'->(field->>'name'));
              ELSIF field ? 'default' THEN values_obj:=values_obj||jsonb_build_object(field->>'name',field->'default'); END IF;
            END IF;
          END LOOP;
        END IF;
      END IF;
      prepared:=prepared||jsonb_build_array(jsonb_set(row_value,'{values}',values_obj));
    END LOOP;
    current_head:=managed.raw_apply_rows(dataset_value,p_version,current_head,prepared,(edit->>'replace')::boolean);
  END LOOP;
  -- Validate the whole candidate before returning, including reordered deletes
  -- and inserts. Leave these managed constraints immediate for the caller.
  FOR constraint_row IN SELECT c.conname FROM pg_constraint c
    JOIN pg_class t ON t.oid=c.conrelid
    JOIN pg_namespace n ON n.oid=t.relnamespace
    JOIN managed.dataset d ON t.relname='d_'||replace(d.dataset_id::text,'-','')
    WHERE n.nspname='managed' AND d.group_id=group_value AND c.condeferrable
      AND position(('db02_'||replace(d.dataset_id::text,'-','')||'_') IN c.conname::text)=1 LOOP
    EXECUTE format('SET CONSTRAINTS managed.%I IMMEDIATE',constraint_row.conname);
  END LOOP;
  RETURN current_head;
END $$;
REVOKE ALL ON FUNCTION managed.apply_group(uuid,uuid,jsonb) FROM PUBLIC;

-- Migration installs snapshot physical tables under each group guard.
UPDATE managed.version_group SET schema_generation=schema_generation+1;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA managed FROM PUBLIC;
