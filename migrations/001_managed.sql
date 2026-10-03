-- SPDX-License-Identifier: GPL-3.0-or-later
-- Applied transactionally by managed.migrate; not an adoption/upgrade of FND-04.
CREATE TABLE managed.version_group (
  group_id uuid PRIMARY KEY,
  default_version_id uuid NOT NULL UNIQUE,
  schema_generation bigint NOT NULL DEFAULT 1 CHECK (schema_generation > 0)
);
CREATE TABLE managed.version (
  version_id uuid PRIMARY KEY,
  group_id uuid NOT NULL REFERENCES managed.version_group,
  name text NOT NULL CHECK (name='DEFAULT'),
  head_revision uuid NOT NULL,
  UNIQUE (group_id,name), UNIQUE (group_id,version_id)
);
ALTER TABLE managed.version_group ADD CONSTRAINT default_version_group
  FOREIGN KEY (group_id,default_version_id) REFERENCES managed.version(group_id,version_id)
  DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE managed.dataset (
  dataset_id uuid PRIMARY KEY,
  managed_name text NOT NULL UNIQUE CHECK (managed_name ~ '^[a-z][a-z0-9_]{0,62}$'),
  catalog_item_id uuid NOT NULL,
  policy_ref uuid NOT NULL,
  group_id uuid NOT NULL REFERENCES managed.version_group,
  active_schema_revision uuid NOT NULL UNIQUE,
  UNIQUE (dataset_id,group_id)
);
CREATE TABLE managed.schema_revision (
  schema_revision_id uuid PRIMARY KEY,
  dataset_id uuid NOT NULL REFERENCES managed.dataset,
  canonical_encoding text NOT NULL CHECK (canonical_encoding='ambisgis-typed-schema-json-v1'),
  canonical_text text NOT NULL,
  definition jsonb NOT NULL,
  schema_sha256 text NOT NULL CHECK (schema_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (canonical_text::jsonb=definition),
  CHECK (encode(sha256(convert_to(canonical_text,'UTF8')),'hex')=schema_sha256),
  UNIQUE(dataset_id,schema_revision_id)
);
ALTER TABLE managed.dataset ADD CONSTRAINT active_schema_dataset
  FOREIGN KEY (dataset_id,active_schema_revision) REFERENCES managed.schema_revision(dataset_id,schema_revision_id)
  DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE managed.feature_identity (
  dataset_id uuid NOT NULL REFERENCES managed.dataset,
  fid uuid NOT NULL,
  object_id bigint NOT NULL CHECK (object_id > 0),
  PRIMARY KEY(dataset_id,fid), UNIQUE(dataset_id,object_id), UNIQUE(dataset_id,fid,object_id)
);
CREATE FUNCTION managed.immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN RAISE EXCEPTION 'IMMUTABLE_MANAGED_IDENTITY'; END $$;
CREATE TRIGGER immutable_mapping BEFORE UPDATE OR DELETE ON managed.feature_identity
  FOR EACH ROW EXECUTE FUNCTION managed.immutable();
CREATE TRIGGER immutable_schema BEFORE UPDATE OR DELETE ON managed.schema_revision
  FOR EACH ROW EXECUTE FUNCTION managed.immutable();
CREATE TRIGGER immutable_dataset_namespace BEFORE UPDATE OF dataset_id,group_id,active_schema_revision ON managed.dataset
  FOR EACH ROW EXECUTE FUNCTION managed.immutable();
CREATE TRIGGER immutable_group_identity BEFORE UPDATE OF group_id,default_version_id ON managed.version_group
  FOR EACH ROW EXECUTE FUNCTION managed.immutable();
CREATE TRIGGER immutable_version_identity BEFORE UPDATE OF version_id,group_id,name ON managed.version
  FOR EACH ROW EXECUTE FUNCTION managed.immutable();
CREATE FUNCTION managed.geometry_finite(g public.geometry) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path=pg_catalog AS $$
  SELECT NOT EXISTS (SELECT 1 FROM public.ST_DumpPoints(g) AS p WHERE
    NOT(abs(public.ST_X(p.geom)) < 'Infinity'::float8) OR
    NOT(abs(public.ST_Y(p.geom)) < 'Infinity'::float8) OR
    NOT(coalesce(abs(public.ST_Z(p.geom)) < 'Infinity'::float8,true)) OR
    NOT(coalesce(abs(public.ST_M(p.geom)) < 'Infinity'::float8,true)))
$$;

-- This is a trusted service mutation primitive. The caller must already have
-- performed live catalog authorization. Ordinary users receive no EXECUTE.
-- Full rows only; no arbitrary expressions, field patches, rules or DDL.
CREATE FUNCTION managed.apply_rows(p_dataset uuid, p_version uuid, p_expected uuid,
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
REVOKE ALL ON ALL TABLES IN SCHEMA managed FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA managed FROM PUBLIC;
