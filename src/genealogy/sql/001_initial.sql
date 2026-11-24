BEGIN;
CREATE SCHEMA IF NOT EXISTS genealogy;
CREATE SCHEMA IF NOT EXISTS publication;
CREATE SCHEMA IF NOT EXISTS authz;
CREATE SCHEMA IF NOT EXISTS operations;

CREATE TABLE IF NOT EXISTS genealogy.entity_stage (
  id text NOT NULL, entity_type text NOT NULL, visibility text NOT NULL,
  document jsonb NOT NULL, retired boolean NOT NULL, generation text NOT NULL,
  PRIMARY KEY (generation, id),
  CHECK (visibility IN ('public','family','private'))
);
CREATE TABLE IF NOT EXISTS genealogy.projection_stage (
  audience text NOT NULL CHECK (audience IN ('public','family')),
  document jsonb NOT NULL, generation text NOT NULL,
  PRIMARY KEY (generation, audience)
);
CREATE TABLE IF NOT EXISTS genealogy.active_generation (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), generation text NOT NULL
);
CREATE TABLE IF NOT EXISTS operations.import_run (
  generation text PRIMARY KEY, corpus_commit char(40) NOT NULL, schema_version integer NOT NULL,
  row_count integer NOT NULL, imported_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS authz.membership (
  subject_id text PRIMARY KEY, family_access boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION genealogy.prepare_generation(wanted text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,genealogy AS $$
BEGIN
  DELETE FROM genealogy.entity_stage WHERE generation=wanted;
  DELETE FROM genealogy.projection_stage WHERE generation=wanted;
END $$;

CREATE OR REPLACE FUNCTION genealogy.activate_generation(wanted text, commit_sha text, version integer, rows integer) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,genealogy,operations AS $$
BEGIN
  IF (SELECT count(*) FROM genealogy.projection_stage WHERE generation=wanted) <> 2 THEN
    RAISE EXCEPTION 'generation lacks both projections';
  END IF;
  INSERT INTO genealogy.active_generation(singleton,generation) VALUES(true,wanted)
    ON CONFLICT(singleton) DO UPDATE SET generation=EXCLUDED.generation;
  INSERT INTO operations.import_run(generation,corpus_commit,schema_version,row_count)
    VALUES(wanted,commit_sha,version,rows) ON CONFLICT(generation) DO NOTHING;
  DELETE FROM genealogy.entity_stage WHERE generation<>wanted;
  DELETE FROM genealogy.projection_stage WHERE generation<>wanted;
END $$;

CREATE OR REPLACE VIEW genealogy.entity AS
 SELECT e.id,e.entity_type,e.visibility,e.document,e.retired FROM genealogy.entity_stage e
 JOIN genealogy.active_generation a USING(generation);
CREATE OR REPLACE VIEW publication.public_document AS
 SELECT p.document FROM genealogy.projection_stage p JOIN genealogy.active_generation a USING(generation)
 WHERE p.audience='public';
CREATE OR REPLACE VIEW publication.family_document AS
 SELECT p.document FROM genealogy.projection_stage p JOIN genealogy.active_generation a USING(generation)
 WHERE p.audience='family';

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='genealogy_importer') THEN CREATE ROLE genealogy_importer NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='genealogy_public') THEN CREATE ROLE genealogy_public NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='genealogy_family') THEN CREATE ROLE genealogy_family NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='genealogy_app') THEN CREATE ROLE genealogy_app NOLOGIN; END IF;
END $$;
REVOKE ALL ON SCHEMA genealogy,publication,authz,operations FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA genealogy,publication,authz,operations FROM PUBLIC;
GRANT USAGE ON SCHEMA genealogy,operations TO genealogy_importer;
GRANT SELECT,INSERT,DELETE ON genealogy.entity_stage,genealogy.projection_stage TO genealogy_importer;
GRANT EXECUTE ON FUNCTION genealogy.prepare_generation(text),genealogy.activate_generation(text,text,integer,integer) TO genealogy_importer;
GRANT USAGE ON SCHEMA publication TO genealogy_public,genealogy_family;
GRANT SELECT ON publication.public_document TO genealogy_public,genealogy_family;
GRANT SELECT ON publication.family_document TO genealogy_family;
GRANT USAGE ON SCHEMA authz TO genealogy_app;
GRANT SELECT(subject_id,family_access) ON authz.membership TO genealogy_app;
COMMIT;
