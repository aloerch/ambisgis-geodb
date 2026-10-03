-- SPDX-License-Identifier: GPL-3.0-or-later
-- FND-04 experimental schema. Install only in a newly created disposable database.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA geodb;
REVOKE ALL ON SCHEMA geodb FROM PUBLIC;
SET search_path = geodb, public;
ALTER DEFAULT PRIVILEGES IN SCHEMA geodb REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE version_group (id integer PRIMARY KEY CHECK(id=1), generation integer NOT NULL);
INSERT INTO version_group VALUES (1, 1);
CREATE TABLE snapshots (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), sealed boolean NOT NULL DEFAULT false,
  source_head bigint NOT NULL, generation integer NOT NULL);
CREATE TABLE versions (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text UNIQUE NOT NULL,
  head bigint NOT NULL DEFAULT 0, base uuid REFERENCES snapshots, generation integer NOT NULL DEFAULT 1);
INSERT INTO versions(id,name) VALUES ('00000000-0000-0000-0000-000000000001','DEFAULT');
CREATE TABLE events (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, version_id uuid REFERENCES versions,
  head bigint NOT NULL, operation text NOT NULL, request_id uuid, actor name NOT NULL DEFAULT session_user,
  occurred_at timestamptz NOT NULL DEFAULT transaction_timestamp());
CREATE TABLE outbox (event_id bigint PRIMARY KEY REFERENCES events, delivered boolean NOT NULL DEFAULT false);
CREATE TABLE plans (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), source uuid NOT NULL REFERENCES versions,
  source_head bigint NOT NULL, target_head bigint NOT NULL, generation integer NOT NULL,
  target_snapshot uuid NOT NULL REFERENCES snapshots, candidate uuid NOT NULL REFERENCES snapshots,
  status text NOT NULL CHECK(status IN ('prepared','accepted','posted')), accepted_head bigint);
CREATE TABLE conflicts (plan_id uuid REFERENCES plans, dataset text NOT NULL, feature_id uuid NOT NULL,
  fields text[] NOT NULL, base jsonb, ours jsonb, theirs jsonb, PRIMARY KEY(plan_id,dataset,feature_id));
CREATE TABLE requests (request_id uuid PRIMARY KEY, actor name NOT NULL DEFAULT session_user,
  payload jsonb NOT NULL, result bigint NOT NULL);

-- Two independent typed datasets form the prototype's one atomic version group.
DO $$ DECLARE d text; BEGIN
  FOREACH d IN ARRAY ARRAY['assets','observations'] LOOP
    EXECUTE format('CREATE TABLE geodb.current_%I (
      version_id uuid NOT NULL REFERENCES geodb.versions, feature_id uuid NOT NULL,
      name text NOT NULL CHECK(length(name) BETWEEN 1 AND 200), value numeric(14,3) NOT NULL CHECK(value>=0),
      geom geometry(PointZ,4326) NOT NULL, PRIMARY KEY(version_id,feature_id),
      UNIQUE(version_id,name) DEFERRABLE INITIALLY DEFERRED)', d);
    EXECUTE format('CREATE INDEX ON geodb.current_%I USING gist(geom)', d);
    EXECUTE format('CREATE TABLE geodb.snapshot_%I (
      snapshot_id uuid NOT NULL REFERENCES geodb.snapshots, feature_id uuid NOT NULL,
      name text NOT NULL CHECK(length(name) BETWEEN 1 AND 200), value numeric(14,3) NOT NULL CHECK(value>=0),
      geom geometry(PointZ,4326) NOT NULL, PRIMARY KEY(snapshot_id,feature_id),
      UNIQUE(snapshot_id,name) DEFERRABLE INITIALLY DEFERRED)', d);
  END LOOP;
END $$;

CREATE FUNCTION guard_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (TG_OP <> 'INSERT' AND (SELECT sealed FROM snapshots WHERE id=OLD.snapshot_id)) OR
     (TG_OP <> 'DELETE' AND (SELECT sealed FROM snapshots WHERE id=NEW.snapshot_id)) THEN
    RAISE EXCEPTION 'IMMUTABLE_SNAPSHOT';
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER immutable_assets BEFORE INSERT OR UPDATE OR DELETE ON snapshot_assets
FOR EACH ROW EXECUTE FUNCTION guard_snapshot();
CREATE TRIGGER immutable_observations BEFORE INSERT OR UPDATE OR DELETE ON snapshot_observations
FOR EACH ROW EXECUTE FUNCTION guard_snapshot();

CREATE FUNCTION require_repeatable_read() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('transaction_isolation') NOT IN ('repeatable read','serializable') THEN
    RAISE EXCEPTION 'REPEATABLE_READ_REQUIRED';
  END IF;
END $$;
CREATE FUNCTION record_event(v uuid, h bigint, op text, req uuid DEFAULT NULL) RETURNS void
LANGUAGE plpgsql AS $$ DECLARE e bigint; BEGIN
  INSERT INTO events(version_id,head,operation,request_id) VALUES(v,h,op,req) RETURNING id INTO e;
  INSERT INTO outbox VALUES(e,false);
END $$;
CREATE FUNCTION capture(v uuid) RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE s uuid; d text; BEGIN
  PERFORM require_repeatable_read();
  INSERT INTO snapshots(source_head,generation) SELECT head,generation FROM versions WHERE id=v RETURNING id INTO s;
  IF s IS NULL THEN RAISE EXCEPTION 'UNKNOWN_VERSION'; END IF;
  FOREACH d IN ARRAY ARRAY['assets','observations'] LOOP
    EXECUTE format('INSERT INTO snapshot_%I SELECT $1,feature_id,name,value,geom FROM current_%I WHERE version_id=$2',d,d) USING s,v;
  END LOOP;
  UPDATE snapshots SET sealed=true WHERE id=s;
  RETURN s;
END $$;
CREATE FUNCTION create_branch(branch_name text) RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE v uuid; s uuid; d text; BEGIN
  PERFORM require_repeatable_read();
  PERFORM 1 FROM version_group WHERE id=1 FOR SHARE;
  s := capture('00000000-0000-0000-0000-000000000001');
  INSERT INTO versions(name,base,generation) SELECT branch_name,s,generation FROM version_group RETURNING id INTO v;
  FOREACH d IN ARRAY ARRAY['assets','observations'] LOOP
    EXECUTE format('INSERT INTO current_%I SELECT $1,feature_id,name,value,geom FROM snapshot_%I WHERE snapshot_id=$2',d,d) USING v,s;
  END LOOP;
  PERFORM record_event(v,0,'create');
  RETURN v;
END $$;

-- Full-row edits, not public API patch semantics. Caller owns one bounded transaction.
CREATE FUNCTION edit(v uuid, expected bigint, dataset text, operation text, fid uuid,
  n text DEFAULT NULL, val numeric DEFAULT NULL, wkt text DEFAULT NULL) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE h bigint; changed integer; BEGIN
  IF dataset NOT IN ('assets','observations') OR operation NOT IN ('insert','update','delete') THEN
    RAISE EXCEPTION 'INVALID_OPERATION';
  END IF;
  SELECT head INTO h FROM versions WHERE id=v FOR UPDATE;
  IF h IS NULL OR expected IS NULL OR h <> expected THEN RAISE EXCEPTION 'STALE_BRANCH_HEAD'; END IF;
  IF operation='delete' THEN
    EXECUTE format('DELETE FROM current_%I WHERE version_id=$1 AND feature_id=$2',dataset) USING v,fid;
  ELSIF operation='update' THEN
    EXECUTE format('UPDATE current_%I SET name=$3,value=$4,geom=ST_GeomFromEWKT($5) WHERE version_id=$1 AND feature_id=$2',dataset)
      USING v,fid,n,val,wkt;
  ELSE
    EXECUTE format('INSERT INTO current_%I VALUES($1,$2,$3,$4,ST_GeomFromEWKT($5))',dataset) USING v,fid,n,val,wkt;
  END IF;
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed <> 1 THEN RAISE EXCEPTION 'UNKNOWN_FEATURE'; END IF;
  UPDATE versions SET head=head+1 WHERE id=v RETURNING head INTO h;
  PERFORM record_event(v,h,'edit');
  RETURN h;
END $$;

-- JSON is a transient full-state comparison representation; persisted features remain typed.
CREATE FUNCTION merge_row(b jsonb, o jsonb, t jsonb) RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE result jsonb='{}'; f text; conflicts text[]='{}'; BEGIN
  IF o IS NOT DISTINCT FROM b THEN RETURN jsonb_build_object('row',t); END IF;
  IF t IS NOT DISTINCT FROM b OR o IS NOT DISTINCT FROM t THEN RETURN jsonb_build_object('row',o); END IF;
  IF b IS NULL OR o IS NULL OR t IS NULL THEN RETURN jsonb_build_object('conflicts',ARRAY['existence']); END IF;
  FOREACH f IN ARRAY ARRAY['name','value','geom'] LOOP
    IF o->f IS NOT DISTINCT FROM b->f THEN result=result || jsonb_build_object(f,t->f);
    ELSIF t->f IS NOT DISTINCT FROM b->f OR o->f IS NOT DISTINCT FROM t->f THEN result=result || jsonb_build_object(f,o->f);
    ELSE conflicts=array_append(conflicts,f); END IF;
  END LOOP;
  IF cardinality(conflicts)>0 THEN RETURN jsonb_build_object('conflicts',conflicts); END IF;
  RETURN jsonb_build_object('row',result);
END $$;

CREATE FUNCTION prepare_reconcile(v uuid) RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE p uuid; s uuid; c uuid; d text; base_id uuid; sh bigint; th bigint; g integer; BEGIN
  PERFORM require_repeatable_read();
  IF v='00000000-0000-0000-0000-000000000001' THEN RAISE EXCEPTION 'DEFAULT_IS_NOT_BRANCH'; END IF;
  PERFORM 1 FROM version_group WHERE id=1 FOR SHARE;
  SELECT head,base,generation INTO sh,base_id,g FROM versions WHERE id=v;
  IF base_id IS NULL THEN RAISE EXCEPTION 'UNKNOWN_BRANCH'; END IF;
  SELECT head INTO th FROM versions WHERE id='00000000-0000-0000-0000-000000000001';
  s := capture('00000000-0000-0000-0000-000000000001');
  INSERT INTO snapshots(source_head,generation) VALUES(sh,g) RETURNING id INTO c;
  INSERT INTO plans(source,source_head,target_head,generation,target_snapshot,candidate,status)
    VALUES(v,sh,th,g,s,c,'prepared') RETURNING id INTO p;
  FOREACH d IN ARRAY ARRAY['assets','observations'] LOOP
    EXECUTE format('WITH b AS (SELECT feature_id,jsonb_build_object(''name'',name,''value'',value,''geom'',encode(ST_AsEWKB(geom,''NDR''),''hex'')) r FROM snapshot_%1$I WHERE snapshot_id=$1),
      o AS (SELECT feature_id,jsonb_build_object(''name'',name,''value'',value,''geom'',encode(ST_AsEWKB(geom,''NDR''),''hex'')) r FROM current_%1$I WHERE version_id=$2),
      t AS (SELECT feature_id,jsonb_build_object(''name'',name,''value'',value,''geom'',encode(ST_AsEWKB(geom,''NDR''),''hex'')) r FROM snapshot_%1$I WHERE snapshot_id=$3),
      m AS MATERIALIZED (SELECT coalesce(b.feature_id,o.feature_id,t.feature_id) fid,b.r b,o.r o,t.r t,merge_row(b.r,o.r,t.r) result FROM b FULL JOIN o USING(feature_id) FULL JOIN t USING(feature_id)),
      errors AS (INSERT INTO conflicts SELECT $4,$5,fid,ARRAY(SELECT jsonb_array_elements_text(result->''conflicts'')),b,o,t FROM m WHERE result ? ''conflicts'')
      INSERT INTO snapshot_%1$I SELECT $6,fid,result->''row''->>''name'',(result->''row''->>''value'')::numeric,
        ST_GeomFromEWKB(decode(result->''row''->>''geom'',''hex'')) FROM m
        WHERE NOT result ? ''conflicts'' AND result->''row'' <> ''null''::jsonb',d)
      USING base_id,v,s,p,d,c;
  END LOOP;
  -- Conflict-free candidates are sealed immediately. Conflicted ones need explicit resolution.
  IF NOT EXISTS(SELECT 1 FROM conflicts WHERE plan_id=p) THEN UPDATE snapshots SET sealed=true WHERE id=c; END IF;
  RETURN p;
END $$;

CREATE FUNCTION resolve_conflict(p uuid, dataset text, fid uuid, choice text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE r jsonb; c uuid; BEGIN
  IF dataset NOT IN ('assets','observations') OR choice NOT IN ('ours','theirs','base') THEN RAISE EXCEPTION 'INVALID_RESOLUTION'; END IF;
  SELECT candidate INTO c FROM plans WHERE id=p AND status='prepared' FOR UPDATE;
  IF c IS NULL THEN RAISE EXCEPTION 'INVALID_PLAN'; END IF;
  SELECT CASE choice WHEN 'ours' THEN ours WHEN 'theirs' THEN theirs ELSE base END INTO r
    FROM conflicts WHERE plan_id=p AND conflicts.dataset=resolve_conflict.dataset AND feature_id=fid;
  IF NOT FOUND THEN RAISE EXCEPTION 'UNKNOWN_CONFLICT'; END IF;
  IF r IS NOT NULL THEN
    EXECUTE format('INSERT INTO snapshot_%I VALUES($1,$2,$3,$4,ST_GeomFromEWKB(decode($5,''hex'')))',dataset)
      USING c,fid,r->>'name',(r->>'value')::numeric,r->>'geom';
  END IF;
  DELETE FROM conflicts WHERE plan_id=p AND conflicts.dataset=resolve_conflict.dataset AND feature_id=fid;
  IF NOT EXISTS(SELECT 1 FROM conflicts WHERE plan_id=p) THEN UPDATE snapshots SET sealed=true WHERE id=c; END IF;
END $$;

CREATE FUNCTION lock_plan(p uuid) RETURNS plans LANGUAGE plpgsql AS $$
DECLARE r plans; sh bigint; th bigint; g integer; BEGIN
  -- All accept/post operations use this exact group -> versions(UUID order) -> plan order.
  PERFORM 1 FROM version_group WHERE id=1 FOR SHARE;
  SELECT * INTO r FROM plans WHERE id=p;
  IF NOT FOUND THEN RAISE EXCEPTION 'UNKNOWN_PLAN'; END IF;
  PERFORM 1 FROM versions WHERE id IN (r.source,'00000000-0000-0000-0000-000000000001') ORDER BY id FOR UPDATE;
  SELECT * INTO r FROM plans WHERE id=p FOR UPDATE;
  SELECT head INTO sh FROM versions WHERE id=r.source;
  SELECT head INTO th FROM versions WHERE id='00000000-0000-0000-0000-000000000001';
  SELECT generation INTO g FROM version_group WHERE id=1;
  IF sh <> (CASE WHEN r.status='prepared' THEN r.source_head ELSE r.accepted_head END) OR th <> r.target_head OR g<>r.generation THEN
    RAISE EXCEPTION 'STALE_RECONCILE';
  END IF;
  IF EXISTS(SELECT 1 FROM conflicts WHERE plan_id=p) OR NOT (SELECT sealed FROM snapshots WHERE id=r.candidate) THEN
    RAISE EXCEPTION 'UNRESOLVED_CONFLICT';
  END IF;
  RETURN r;
END $$;

CREATE FUNCTION apply_snapshot(v uuid, s uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE d text; BEGIN
  FOREACH d IN ARRAY ARRAY['assets','observations'] LOOP
    EXECUTE format('DELETE FROM current_%1$I c WHERE c.version_id=$1 AND NOT EXISTS(SELECT 1 FROM snapshot_%1$I s WHERE s.snapshot_id=$2 AND s.feature_id=c.feature_id)',d) USING v,s;
    EXECUTE format('INSERT INTO current_%1$I SELECT $1,s.feature_id,s.name,s.value,s.geom FROM snapshot_%1$I s
      LEFT JOIN current_%1$I old ON old.version_id=$1 AND old.feature_id=s.feature_id
      WHERE s.snapshot_id=$2 AND (old.feature_id IS NULL OR
        (old.name,old.value,ST_AsEWKB(old.geom)) IS DISTINCT FROM (s.name,s.value,ST_AsEWKB(s.geom)))
      ON CONFLICT(version_id,feature_id) DO UPDATE SET name=EXCLUDED.name,value=EXCLUDED.value,geom=EXCLUDED.geom
      WHERE (current_%1$I.name,current_%1$I.value,ST_AsEWKB(current_%1$I.geom)) IS DISTINCT FROM (EXCLUDED.name,EXCLUDED.value,ST_AsEWKB(EXCLUDED.geom))',d) USING v,s;
  END LOOP;
END $$;
CREATE FUNCTION accept_reconcile(p uuid) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE r plans; h bigint; BEGIN
  r:=lock_plan(p);
  IF r.status<>'prepared' THEN RAISE EXCEPTION 'INVALID_PLAN_STATE'; END IF;
  PERFORM apply_snapshot(r.source,r.candidate);
  UPDATE versions SET head=head+1,base=r.target_snapshot WHERE id=r.source RETURNING head INTO h;
  UPDATE plans SET status='accepted',accepted_head=h WHERE id=p;
  PERFORM record_event(r.source,h,'accept');
  RETURN h;
END $$;
CREATE FUNCTION post(p uuid, expected_source bigint, expected_target bigint, request_id uuid) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE r plans; h bigint; payload jsonb; previous requests; BEGIN
  payload:=jsonb_build_array(p,expected_source,expected_target);
  -- Serialize duplicate request IDs without widening to every unrelated post.
  PERFORM pg_advisory_xact_lock(hashtextextended(request_id::text,0));
  SELECT * INTO previous FROM requests WHERE requests.request_id=post.request_id;
  IF FOUND THEN
    IF previous.payload<>payload OR previous.actor<>session_user THEN RAISE EXCEPTION 'IDEMPOTENCY_CONFLICT'; END IF;
    RETURN previous.result;
  END IF;
  r:=lock_plan(p);
  IF r.status<>'accepted' OR expected_source IS NULL OR expected_target IS NULL OR expected_source<>r.accepted_head OR expected_target<>r.target_head THEN RAISE EXCEPTION 'STALE_RECONCILE'; END IF;
  PERFORM apply_snapshot('00000000-0000-0000-0000-000000000001',r.candidate);
  UPDATE versions SET head=head+1 WHERE id='00000000-0000-0000-0000-000000000001' RETURNING head INTO h;
  UPDATE versions SET head=head+1,base=r.candidate WHERE id=r.source;
  UPDATE plans SET status='posted' WHERE id=p;
  PERFORM record_event('00000000-0000-0000-0000-000000000001',h,'post',request_id);
  PERFORM record_event(r.source,r.accepted_head+1,'checkpoint',request_id);
  INSERT INTO requests VALUES(request_id,session_user,payload,h);
  RETURN h;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA geodb FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA geodb FROM PUBLIC;
-- Intentionally no service grants or SECURITY DEFINER bypass. Security integration is a later task.
