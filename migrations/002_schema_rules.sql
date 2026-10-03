-- SPDX-License-Identifier: GPL-3.0-or-later
-- DB-02 forward migration. Existing v1 hashes and identities remain unchanged.
ALTER TABLE managed.schema_revision DROP CONSTRAINT schema_revision_canonical_encoding_check;
ALTER TABLE managed.schema_revision ADD CHECK
  (canonical_encoding IN ('ambisgis-typed-schema-json-v1','ambisgis-typed-schema-json-v2'));
DROP TRIGGER immutable_dataset_namespace ON managed.dataset;
CREATE TRIGGER immutable_dataset_namespace BEFORE UPDATE OF dataset_id,group_id ON managed.dataset
  FOR EACH ROW EXECUTE FUNCTION managed.immutable();
CREATE FUNCTION managed.raw_apply_rows(p_dataset uuid, p_version uuid, p_expected uuid,
                                  p_rows jsonb, p_replace boolean)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp AS $$
DECLARE
  ds record; current_head uuid; new_head uuid; row_value jsonb; values_obj jsonb;
  field jsonb; scalar jsonb; scalar_text text; fid_value uuid; oid_value bigint;
  table_name text; sequence_name text; column_sql text; value_sql text; update_sql text;
  kind text; type_sql text; name_sql text; seen uuid[] := ARRAY[]::uuid[];
  geometry_def jsonb; allowed_keys text[]; definition_value jsonb;
BEGIN
  IF p_dataset IS NULL OR p_version IS NULL OR p_expected IS NULL OR p_replace IS NULL
     OR p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' THEN
    RAISE EXCEPTION 'INVALID_MANAGED_REQUEST';
  END IF;
  IF jsonb_array_length(p_rows)>10000 THEN RAISE EXCEPTION 'MANAGED_BATCH_LIMIT'; END IF;
  SELECT d.*, r.definition, g.default_version_id INTO ds
    FROM managed.dataset d JOIN managed.schema_revision r ON r.schema_revision_id=d.active_schema_revision
    JOIN managed.version_group g ON g.group_id=d.group_id WHERE d.dataset_id=p_dataset;
  IF NOT FOUND OR ds.default_version_id<>p_version THEN RAISE EXCEPTION 'DATASET_VERSION_MISMATCH'; END IF;
  SELECT head_revision INTO current_head FROM managed.version
    WHERE version_id=p_version AND group_id=ds.group_id FOR UPDATE;
  IF current_head IS DISTINCT FROM p_expected THEN RAISE EXCEPTION 'STALE_HEAD'; END IF;
  table_name := 'd_'||replace(p_dataset::text,'-','');
  sequence_name := 'oid_'||replace(p_dataset::text,'-','');
  geometry_def := ds.definition->'geometry';
  allowed_keys := CASE WHEN geometry_def='null'::jsonb THEN ARRAY['fid','values'] ELSE ARRAY['fid','values','geometry'] END;
  FOR row_value IN SELECT value FROM jsonb_array_elements(p_rows) LOOP
    IF jsonb_typeof(row_value)<>'object' OR
       (SELECT count(*) FROM jsonb_object_keys(row_value)) <> cardinality(allowed_keys) OR
       EXISTS(SELECT 1 FROM jsonb_object_keys(row_value) k WHERE NOT k=ANY(allowed_keys)) OR
       jsonb_typeof(row_value->'fid') IS DISTINCT FROM 'string' OR
       (row_value->>'fid') !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' OR
       jsonb_typeof(row_value->'values') IS DISTINCT FROM 'object' THEN
      RAISE EXCEPTION 'INVALID_FEATURE_ROW';
    END IF;
    fid_value := (row_value->>'fid')::uuid;
    IF fid_value=ANY(seen) THEN RAISE EXCEPTION 'DUPLICATE_FEATURE_UUID'; END IF;
    seen := array_append(seen,fid_value);
    values_obj := row_value->'values';
    IF (SELECT count(*) FROM jsonb_object_keys(values_obj)) <> jsonb_array_length(ds.definition->'fields') THEN
      RAISE EXCEPTION 'FIELD_SET_MISMATCH';
    END IF;
    column_sql := 'dataset_id,version_group_id,version_id,fid,object_id,feature_revision';
    -- Registry lookup precedes allocation: republication never consumes a new
    -- identity for an existing UUID. Sequence gaps on rollback are intentional.
    SELECT object_id INTO oid_value FROM managed.feature_identity WHERE dataset_id=p_dataset AND fid=fid_value;
    IF NOT FOUND THEN
      oid_value := nextval(format('managed.%I',sequence_name)::regclass);
      INSERT INTO managed.feature_identity VALUES(p_dataset,fid_value,oid_value);
    END IF;
    value_sql := format('%L::uuid,%L::uuid,%L::uuid,%L::uuid,%L::bigint,gen_random_uuid()',
                         p_dataset,ds.group_id,p_version,fid_value,oid_value);
    update_sql := 'feature_revision=EXCLUDED.feature_revision';
    FOR field IN SELECT value FROM jsonb_array_elements(ds.definition->'fields') LOOP
      IF NOT values_obj ? (field->>'name') THEN RAISE EXCEPTION 'FIELD_SET_MISMATCH'; END IF;
      scalar := values_obj->(field->>'name'); kind := field->>'type';
      IF scalar='null'::jsonb THEN
        IF NOT (field->>'nullable')::boolean THEN RAISE EXCEPTION 'NULL_NOT_ALLOWED'; END IF;
        scalar_text := NULL;
      ELSE
        scalar_text := scalar #>> '{}';
        IF kind='string' AND jsonb_typeof(scalar)<>'string' OR
           kind='boolean' AND jsonb_typeof(scalar)<>'boolean' OR
           kind='int32' AND (jsonb_typeof(scalar)<>'number' OR scalar_text !~ '^-?(0|[1-9][0-9]*)$') OR
           kind='int64' AND (jsonb_typeof(scalar)<>'string' OR scalar_text !~ '^-?(0|[1-9][0-9]*)$') OR
           kind='decimal' AND (jsonb_typeof(scalar)<>'string' OR scalar_text !~ '^-?(0|[1-9][0-9]*)(\.[0-9]+)?$') OR
           kind='uuid' AND (jsonb_typeof(scalar)<>'string' OR scalar_text !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') OR
           kind='date' AND (jsonb_typeof(scalar)<>'string' OR scalar_text !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$') OR
           kind='timestamp' AND (jsonb_typeof(scalar)<>'string' OR scalar_text !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt]([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?([Zz]|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$') THEN
          RAISE EXCEPTION 'INVALID_TYPED_VALUE';
        END IF;
      END IF;
      type_sql := CASE kind WHEN 'string' THEN 'text' WHEN 'int32' THEN 'integer' WHEN 'int64' THEN 'bigint'
        WHEN 'decimal' THEN 'numeric' WHEN 'boolean' THEN 'boolean' WHEN 'uuid' THEN 'uuid'
        WHEN 'date' THEN 'date' WHEN 'timestamp' THEN 'timestamptz' ELSE NULL END;
      IF type_sql IS NULL THEN RAISE EXCEPTION 'INVALID_REGISTERED_SCHEMA'; END IF;
      name_sql := format('%I',field->>'name');
      column_sql := column_sql||','||name_sql;
      value_sql := value_sql||','||format('%L::%s',scalar_text,type_sql);
      update_sql := update_sql||','||name_sql||'=EXCLUDED.'||name_sql;
    END LOOP;
    IF geometry_def<>'null'::jsonb THEN
      IF row_value->'geometry'='null'::jsonb THEN
        value_sql := value_sql||',NULL';
      ELSE
        IF jsonb_typeof(row_value->'geometry')<>'string' OR (row_value->>'geometry') !~ '^SRID=[0-9]+;' THEN RAISE EXCEPTION 'INVALID_GEOMETRY_INPUT'; END IF;
        value_sql := value_sql||format(',public.ST_GeomFromEWKT(%L)',row_value->>'geometry');
      END IF;
      column_sql := column_sql||',geom'; update_sql := update_sql||',geom=EXCLUDED.geom';
    END IF;
    EXECUTE format('INSERT INTO managed.%I (%s) VALUES (%s) ON CONFLICT(version_id,fid) DO UPDATE SET %s',
                   table_name,column_sql,value_sql,update_sql);
  END LOOP;
  IF p_replace THEN
    EXECUTE format('DELETE FROM managed.%I WHERE version_id=$1 AND NOT(fid=ANY($2))',table_name)
      USING p_version,seen;
  END IF;
  new_head := gen_random_uuid();
  UPDATE managed.version SET head_revision=new_head WHERE version_id=p_version;
  RETURN new_head;
END $$;

-- Raw storage helper is never granted to backend roles. The original apply_rows
-- OID is replaced below so existing legitimate service grants stay on the guard.
REVOKE ALL ON FUNCTION managed.raw_apply_rows(uuid,uuid,uuid,jsonb,boolean) FROM PUBLIC;

CREATE FUNCTION managed.apply_group(p_version uuid,p_expected uuid,p_edits jsonb)
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
  -- A single lock order for schema changes, dataset creation and all group writes.
  PERFORM 1 FROM managed.version_group WHERE group_id=group_value FOR UPDATE;
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

CREATE OR REPLACE FUNCTION managed.apply_rows(p_dataset uuid,p_version uuid,p_expected uuid,p_rows jsonb,p_replace boolean)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,managed,pg_temp AS $$
BEGIN
  IF p_dataset IS NULL OR p_version IS NULL OR p_expected IS NULL OR p_replace IS NULL
     OR p_rows IS NULL OR jsonb_typeof(p_rows)<>'array' THEN RAISE EXCEPTION 'INVALID_MANAGED_REQUEST'; END IF;
  IF NOT EXISTS(SELECT 1 FROM managed.dataset d JOIN managed.version_group g ON g.group_id=d.group_id
                WHERE d.dataset_id=p_dataset AND g.default_version_id=p_version) THEN
    RAISE EXCEPTION 'DATASET_VERSION_MISMATCH';
  END IF;
  RETURN managed.apply_group(p_version,p_expected,jsonb_build_array(
    jsonb_build_object('dataset_id',p_dataset,'rows',p_rows,'replace',p_replace)));
END $$;
