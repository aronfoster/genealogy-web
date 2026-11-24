-- Tokens contain only the authorized compiled projection, never corpus/generation IDs.
-- The trigger also supports older importers during runtime rollback.
ALTER TABLE genealogy.projection_stage ADD COLUMN listing_token text;
UPDATE genealogy.projection_stage
 SET listing_token=encode(sha256(convert_to(document::text,'UTF8')),'hex');
ALTER TABLE genealogy.projection_stage ALTER COLUMN listing_token SET NOT NULL;
CREATE FUNCTION genealogy.set_listing_token() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 NEW.listing_token := encode(sha256(convert_to(NEW.document::text,'UTF8')),'hex');
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION genealogy.set_listing_token() FROM PUBLIC;
CREATE TRIGGER projection_listing_token BEFORE INSERT OR UPDATE ON genealogy.projection_stage
 FOR EACH ROW EXECUTE FUNCTION genealogy.set_listing_token();
GRANT CREATE ON SCHEMA site_public,site_family TO genealogy_projection_owner;
DO $$ DECLARE audience text; ns text; BEGIN
 FOREACH audience IN ARRAY ARRAY['public','family'] LOOP
  ns := 'site_' || audience;
  EXECUTE format('CREATE VIEW %I.listing_token WITH (security_barrier=true) AS SELECT p.audience || '':'' || p.listing_token AS token FROM genealogy.projection_stage p JOIN genealogy.active_generation a USING(generation) WHERE p.audience=%L',ns,audience);
  EXECUTE format('ALTER VIEW %I.listing_token OWNER TO genealogy_projection_owner',ns);
 END LOOP;
END $$;
REVOKE CREATE ON SCHEMA site_public,site_family FROM genealogy_projection_owner;
GRANT SELECT ON site_public.listing_token TO genealogy_public_ro,genealogy_family_ro,genealogy_public,genealogy_family;
GRANT SELECT ON site_family.listing_token TO genealogy_family_ro,genealogy_family;
