-- Keep immutable per-generation provenance and also record every successful swap.
-- Deferred FK permits the initial activation, whose provenance is inserted later
-- by activate_generation in the same transaction. Failed imports record no success.
CREATE TABLE operations.import_event (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  generation text NOT NULL REFERENCES operations.import_run(generation) DEFERRABLE INITIALLY DEFERRED,
  imported_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX import_event_generation ON operations.import_event(generation,id);
REVOKE ALL ON operations.import_event FROM PUBLIC;
CREATE FUNCTION genealogy.record_import_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,operations AS $$
BEGIN
  INSERT INTO operations.import_event(generation) VALUES (NEW.generation);
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION genealogy.record_import_event() FROM PUBLIC;
CREATE TRIGGER record_import_event AFTER INSERT OR UPDATE ON genealogy.active_generation
  FOR EACH ROW EXECUTE FUNCTION genealogy.record_import_event();
