-- FOS-205: no message, email, URL, person ID or credential is stored here.
-- Receipt tombstones have no automatic expiry: deletion would enable replay.
CREATE TABLE authz.feedback_receipt (
 client_key uuid PRIMARY KEY,
 reporter_check text NOT NULL CHECK (reporter_check ~ '^[0-9a-f]{32}$'),
 payload_check text NOT NULL CHECK (payload_check ~ '^[0-9a-f]{64}$'),
 destination_check text NOT NULL CHECK (destination_check ~ '^[0-9a-f]{64}$'),
 state text NOT NULL DEFAULT 'prepared' CHECK (state IN ('prepared','pending','rejected','received')),
 issue_id text CHECK (issue_id ~ '^[0-9]+-[0-9]+$'),
 identifier text CHECK (identifier ~ '^GEN-[1-9][0-9]*$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK ((state='received')=(issue_id IS NOT NULL AND identifier IS NOT NULL)),
 CHECK ((issue_id IS NULL)=(identifier IS NULL))
);
GRANT SELECT,INSERT,UPDATE ON authz.feedback_receipt TO genealogy_authz_owner;
GRANT SELECT ON authz.feedback_receipt TO genealogy_authz_operator;

-- Authz restore quarantine blocks all new creates until the owner recovers a
-- current receipt history. A stale backup cannot prove that an absent UUID is new.
CREATE TABLE authz.feedback_restore_guard (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 blocked_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
GRANT SELECT,INSERT ON authz.feedback_restore_guard TO genealogy_authz_owner;
GRANT SELECT ON authz.feedback_restore_guard TO genealogy_authz_operator;

CREATE FUNCTION authz.feedback_get(p_key uuid) RETURNS SETOF authz.feedback_receipt
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
 SELECT * FROM authz.feedback_receipt WHERE client_key=p_key
$$;

CREATE FUNCTION authz.feedback_claim(p_key uuid,p_reporter text,p_payload text,p_destination text)
RETURNS SETOF authz.feedback_receipt
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
BEGIN
 IF EXISTS (SELECT FROM authz.feedback_restore_guard) THEN
  RAISE EXCEPTION 'feedback restore reconciliation required';
 END IF;
 INSERT INTO authz.feedback_receipt(client_key,reporter_check,payload_check,destination_check)
 VALUES(p_key,p_reporter,p_payload,p_destination) ON CONFLICT DO NOTHING;
 RETURN QUERY SELECT * FROM authz.feedback_receipt WHERE client_key=p_key;
END $$;

CREATE FUNCTION authz.feedback_record(p_key uuid,p_reporter text,p_payload text,p_destination text,
 p_state text,p_issue text,p_identifier text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE changed integer;
BEGIN
 UPDATE authz.feedback_receipt SET state=p_state,issue_id=p_issue,identifier=p_identifier,
 updated_at=clock_timestamp()
 WHERE client_key=p_key AND reporter_check=p_reporter AND payload_check=p_payload
 AND destination_check=p_destination AND (
  (state IN ('prepared','rejected') AND p_state='pending') OR
  (state='pending' AND p_state IN ('rejected','received')));
 GET DIAGNOSTICS changed=ROW_COUNT;
 RETURN changed=1;
END $$;

GRANT CREATE ON SCHEMA authz TO genealogy_authz_owner;
ALTER FUNCTION authz.feedback_get(uuid) OWNER TO genealogy_authz_owner;
ALTER FUNCTION authz.feedback_claim(uuid,text,text,text) OWNER TO genealogy_authz_owner;
ALTER FUNCTION authz.feedback_record(uuid,text,text,text,text,text,text) OWNER TO genealogy_authz_owner;
REVOKE CREATE ON SCHEMA authz FROM genealogy_authz_owner;
REVOKE ALL ON FUNCTION authz.feedback_get(uuid),authz.feedback_claim(uuid,text,text,text),
 authz.feedback_record(uuid,text,text,text,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION authz.feedback_get(uuid),authz.feedback_claim(uuid,text,text,text),
 authz.feedback_record(uuid,text,text,text,text,text,text) TO genealogy_authz_runtime;

CREATE OR REPLACE FUNCTION authz.quarantine_restore(p_actor text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE n integer; o record;
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
 INSERT INTO authz.feedback_restore_guard(singleton) VALUES(true) ON CONFLICT DO NOTHING;
 FOR o IN SELECT id FROM authz.owner_binding WHERE retired_at IS NULL LOOP
  PERFORM authz.owner_retire(o.id,p_actor,'restore quarantine');
 END LOOP;
 WITH changed AS (
 UPDATE authz.member SET reconciled=false,version=version+1,updated_at=clock_timestamp()
 WHERE reconciled RETURNING id,state,version), audited AS (
 INSERT INTO authz.decision(member_id,action,actor,from_state,to_state,version)
 SELECT id,'quarantine-restore',p_actor,state,state,version FROM changed RETURNING id)
 SELECT count(*) INTO n FROM changed;
 RETURN n;
END $$;
