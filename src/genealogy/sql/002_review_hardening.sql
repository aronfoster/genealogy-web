-- Versioned additive migration; works on databases initialized by PR #1.
ALTER TABLE genealogy.entity_stage DROP CONSTRAINT entity_stage_visibility_check;
UPDATE genealogy.entity_stage SET visibility='withheld' WHERE visibility='private';
ALTER TABLE genealogy.entity_stage ADD CHECK (visibility IN ('public','family','admin','withheld'));
ALTER TABLE operations.import_run ALTER COLUMN corpus_commit TYPE text USING trim(corpus_commit);
ALTER TABLE operations.import_run ADD CHECK (corpus_commit ~ '^([0-9a-f]{40}|[0-9a-f]{64})$');
REVOKE ALL ON FUNCTION genealogy.prepare_generation(text),genealogy.activate_generation(text,text,integer,integer) FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA genealogy REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE genealogy.assertion_subject (
 generation text NOT NULL, assertion_id text NOT NULL, role text NOT NULL, ref_id text NOT NULL,
 PRIMARY KEY(generation,assertion_id,role,ref_id),
 FOREIGN KEY(generation,assertion_id) REFERENCES genealogy.entity_stage(generation,id) ON DELETE CASCADE,
 FOREIGN KEY(generation,ref_id) REFERENCES genealogy.entity_stage(generation,id) ON DELETE CASCADE
);
CREATE INDEX assertion_subject_ref ON genealogy.assertion_subject(generation,ref_id,role);
CREATE TABLE genealogy.assertion_evidence (
 generation text NOT NULL, assertion_id text NOT NULL, citation_id text NOT NULL, stance text NOT NULL,
 PRIMARY KEY(generation,assertion_id,citation_id,stance),
 FOREIGN KEY(generation,assertion_id) REFERENCES genealogy.entity_stage(generation,id) ON DELETE CASCADE,
 FOREIGN KEY(generation,citation_id) REFERENCES genealogy.entity_stage(generation,id) ON DELETE CASCADE
);
CREATE INDEX assertion_evidence_citation ON genealogy.assertion_evidence(generation,citation_id);
CREATE TABLE genealogy.publication_decision (
 generation text NOT NULL, id text NOT NULL, visibility text NOT NULL,
 PRIMARY KEY(generation,id),
 FOREIGN KEY(generation,id) REFERENCES genealogy.entity_stage(generation,id) ON DELETE CASCADE
);

-- Typed views retain raw predicate payloads in JSONB. Tree joins use the normalized
-- subject table, not JSON array searches. Raw views remain importer/admin-only.
DO $$ DECLARE kind text; BEGIN
 FOREACH kind IN ARRAY ARRAY['person','persona','assertion','source','citation','place','asset','narrative'] LOOP
  EXECUTE format('CREATE VIEW genealogy.%I AS SELECT id, visibility, document, retired FROM genealogy.entity WHERE entity_type=%L',kind,kind);
 END LOOP;
END $$;

CREATE TABLE genealogy.serving_entity (
 generation text NOT NULL, audience text NOT NULL, id text NOT NULL, category text NOT NULL, document jsonb NOT NULL,
 PRIMARY KEY(generation,audience,id),
 FOREIGN KEY(generation,id) REFERENCES genealogy.entity_stage(generation,id) ON DELETE CASCADE
);
CREATE TABLE genealogy.serving_subject (
 generation text NOT NULL, audience text NOT NULL, assertion_id text NOT NULL, role text NOT NULL, ref_id text NOT NULL,
 PRIMARY KEY(generation,audience,assertion_id,role,ref_id),
 FOREIGN KEY(generation,audience,assertion_id) REFERENCES genealogy.serving_entity(generation,audience,id) ON DELETE CASCADE,
 FOREIGN KEY(generation,audience,ref_id) REFERENCES genealogy.serving_entity(generation,audience,id) ON DELETE CASCADE
);
CREATE INDEX serving_subject_ref ON genealogy.serving_subject(generation,audience,ref_id,role);
CREATE SCHEMA site_public;
CREATE SCHEMA site_family;
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='genealogy_projection_owner') THEN CREATE ROLE genealogy_projection_owner NOLOGIN; END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='genealogy_public_ro') THEN CREATE ROLE genealogy_public_ro NOLOGIN; END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='genealogy_family_ro') THEN CREATE ROLE genealogy_family_ro NOLOGIN; END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='genealogy_app_rw') THEN CREATE ROLE genealogy_app_rw NOLOGIN; END IF;
END $$;
GRANT USAGE ON SCHEMA genealogy TO genealogy_projection_owner;
GRANT SELECT ON genealogy.serving_entity,genealogy.serving_subject,genealogy.projection_stage,genealogy.active_generation TO genealogy_projection_owner;
GRANT USAGE,CREATE ON SCHEMA site_public,site_family,publication TO genealogy_projection_owner;
DO $$ DECLARE audience text; ns text; kind text; category text; BEGIN
 FOREACH audience IN ARRAY ARRAY['public','family'] LOOP
  ns := 'site_' || audience;
  EXECUTE format('CREATE VIEW %I.entity WITH (security_barrier=true) AS SELECT e.id,e.category,e.document FROM genealogy.serving_entity e JOIN genealogy.active_generation a USING(generation) WHERE audience=%L',ns,audience);
  EXECUTE format('ALTER VIEW %I.entity OWNER TO genealogy_projection_owner',ns);
  FOREACH kind IN ARRAY ARRAY['person','persona','assertion','history','alternatives','source','citation','place','asset','narrative'] LOOP
   category := CASE kind WHEN 'person' THEN 'people' WHEN 'history' THEN 'history' WHEN 'alternatives' THEN 'alternatives' ELSE kind || 's' END;
   EXECUTE format('CREATE VIEW %I.%I WITH (security_barrier=true) AS SELECT id,document FROM %I.entity WHERE category=%L',ns,kind,ns,category);
   EXECUTE format('ALTER VIEW %I.%I OWNER TO genealogy_projection_owner',ns,kind);
  END LOOP;
  EXECUTE format('CREATE VIEW %I.assertion_subject WITH (security_barrier=true) AS SELECT s.assertion_id,s.role,s.ref_id FROM genealogy.serving_subject s JOIN genealogy.active_generation a USING(generation) WHERE audience=%L',ns,audience);
  EXECUTE format('ALTER VIEW %I.assertion_subject OWNER TO genealogy_projection_owner',ns);
 END LOOP;
END $$;
ALTER VIEW publication.public_document OWNER TO genealogy_projection_owner;
ALTER VIEW publication.family_document OWNER TO genealogy_projection_owner;
REVOKE CREATE ON SCHEMA site_public,site_family,publication FROM genealogy_projection_owner;
REVOKE ALL ON SCHEMA site_public,site_family FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA site_public,site_family,genealogy FROM PUBLIC;
GRANT USAGE ON SCHEMA site_public TO genealogy_public_ro,genealogy_family_ro,genealogy_public,genealogy_family;
GRANT SELECT ON ALL TABLES IN SCHEMA site_public TO genealogy_public_ro,genealogy_family_ro,genealogy_public,genealogy_family;
GRANT USAGE ON SCHEMA site_family TO genealogy_family_ro,genealogy_family;
GRANT SELECT ON ALL TABLES IN SCHEMA site_family TO genealogy_family_ro,genealogy_family;
GRANT INSERT,SELECT,DELETE ON genealogy.assertion_subject,genealogy.assertion_evidence,genealogy.serving_entity,genealogy.serving_subject,genealogy.publication_decision TO genealogy_importer;
GRANT USAGE ON SCHEMA publication TO genealogy_public_ro,genealogy_family_ro;
GRANT SELECT ON publication.public_document TO genealogy_public_ro,genealogy_family_ro;
GRANT SELECT ON publication.family_document TO genealogy_family_ro;
GRANT USAGE ON SCHEMA authz TO genealogy_app_rw;
GRANT SELECT(subject_id,family_access) ON authz.membership TO genealogy_app_rw;
-- Membership approvals remain operator-owned; the app can only queue requests.
CREATE TABLE authz.access_request (id text PRIMARY KEY, email text NOT NULL, message text NOT NULL, submitted_at timestamptz NOT NULL DEFAULT clock_timestamp());
GRANT INSERT(id,email,message) ON authz.access_request TO genealogy_app_rw;
