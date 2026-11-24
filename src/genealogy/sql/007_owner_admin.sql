-- FOS-127: owner authority is independent of membership and reader credentials.
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='genealogy_authz_owner_web') THEN
  CREATE ROLE genealogy_authz_owner_web NOLOGIN;
 END IF;
END $$;
GRANT USAGE ON SCHEMA authz TO genealogy_authz_owner_web;
CREATE TABLE authz.owner_binding (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 project text NOT NULL CHECK (length(project) BETWEEN 1 AND 128),
 uid text NOT NULL CHECK (length(uid) BETWEEN 1 AND 128),
 normalized_email text NOT NULL CHECK (normalized_email=lower(btrim(normalized_email) COLLATE "C")
  AND octet_length(normalized_email)=length(normalized_email) AND length(normalized_email)<=254
  AND normalized_email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), retired_at timestamptz
);
CREATE UNIQUE INDEX owner_binding_active ON authz.owner_binding(project,uid) WHERE retired_at IS NULL;
CREATE TABLE authz.owner_audit (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES authz.owner_binding(id),
 action text NOT NULL, actor text NOT NULL CHECK(length(actor) BETWEEN 1 AND 200),
 note text NOT NULL DEFAULT '' CHECK(length(note)<=500),
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE authz.owner_operation (
 owner_id uuid NOT NULL REFERENCES authz.owner_binding(id), operation_id uuid NOT NULL,
 payload jsonb NOT NULL, result_version bigint NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(owner_id,operation_id)
);
GRANT SELECT,INSERT,UPDATE ON authz.owner_binding TO genealogy_authz_owner;
GRANT SELECT,INSERT ON authz.owner_audit,authz.owner_operation TO genealogy_authz_owner;
GRANT SELECT ON authz.owner_binding,authz.owner_audit,authz.owner_operation TO genealogy_authz_operator;

CREATE FUNCTION authz.owner_add(p_project text,p_uid text,p_email text,p_actor text,p_note text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE v_id uuid;
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
 SELECT id INTO v_id FROM authz.owner_binding WHERE project=p_project AND uid=p_uid
  AND normalized_email=p_email AND retired_at IS NULL;
 IF FOUND THEN RETURN v_id; END IF;
 -- Retired bindings are not resurrected by retrying an old bootstrap command.
 IF EXISTS(SELECT FROM authz.owner_binding WHERE project=p_project AND uid=p_uid) THEN
  RAISE EXCEPTION 'use a fresh owner identity for recovery';
 END IF;
 INSERT INTO authz.owner_binding(project,uid,normalized_email) VALUES(p_project,p_uid,p_email) RETURNING id INTO v_id;
 INSERT INTO authz.owner_audit(owner_id,action,actor,note) VALUES(v_id,'owner-add',p_actor,p_note);
 RETURN v_id;
END $$;
CREATE FUNCTION authz.owner_retire(p_owner uuid,p_actor text,p_note text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
 UPDATE authz.owner_binding SET retired_at=clock_timestamp() WHERE id=p_owner AND retired_at IS NULL;
 IF NOT FOUND THEN RETURN false; END IF;
 INSERT INTO authz.owner_audit(owner_id,action,actor,note) VALUES(p_owner,'owner-retire',p_actor,p_note);
 RETURN true;
END $$;
CREATE FUNCTION authz.owner_require(p_project text,p_uid text,p_email text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE v_id uuid;
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
 SELECT id INTO v_id FROM authz.owner_binding WHERE project=p_project AND uid=p_uid
  AND normalized_email=p_email AND retired_at IS NULL;
 IF v_id IS NULL THEN RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='not permitted'; END IF;
 RETURN v_id;
END $$;

-- Bounded owner-only directory. Includes pending, revoked and quarantined members,
-- and conflict requests; none are silently converted into approve-able requests.
-- Keep queue/detail in plpgsql: deferred binding permits fresh 007-before-008 installs; 008 creates preauthorization before these functions run.
CREATE FUNCTION authz.owner_queue(p_project text,p_uid text,p_email text,p_all boolean,p_offset integer) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
BEGIN
 PERFORM authz.owner_require(p_project,p_uid,p_email);
 IF p_offset IS NULL OR p_offset<0 OR p_offset>1000000 THEN RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid page'; END IF;
 RETURN coalesce((SELECT jsonb_agg(to_jsonb(q)) FROM (
  SELECT m.*, (SELECT jsonb_agg(e.original_email ORDER BY e.created_at,e.id)
    FROM authz.email_identity e WHERE e.member_id=m.id) AS emails,
   (SELECT jsonb_build_object('email',p.normalized_email,'claimed',p.claimed_at IS NOT NULL)
    FROM authz.preauthorization p WHERE p.member_id=m.id) AS preauthorization,
   (SELECT count(*) FROM authz.family_request r WHERE coalesce(r.member_id,r.conflict_member_id)=m.id AND r.decided_at IS NULL) AS open_requests
  FROM authz.member m WHERE p_all OR m.state='pending' OR NOT m.reconciled OR EXISTS(
   SELECT FROM authz.family_request r WHERE coalesce(r.member_id,r.conflict_member_id)=m.id AND r.decided_at IS NULL)
  ORDER BY m.created_at,m.id LIMIT 51 OFFSET p_offset
 ) q),'[]'::jsonb);
END $$;
CREATE FUNCTION authz.owner_detail(p_project text,p_uid text,p_email text,p_member uuid,p_offset integer) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE m authz.member%ROWTYPE;
BEGIN
 PERFORM authz.owner_require(p_project,p_uid,p_email);
 IF p_offset IS NULL OR p_offset<0 OR p_offset>1000000 THEN RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid page'; END IF;
 SELECT * INTO STRICT m FROM authz.member WHERE id=p_member;
 RETURN jsonb_build_object('member',to_jsonb(m),
  'preauthorization',(SELECT jsonb_build_object('email',p.normalized_email,'claimed',p.claimed_at IS NOT NULL)
    FROM authz.preauthorization p WHERE p.member_id=m.id),
  'identities',(SELECT coalesce(jsonb_agg(to_jsonb(q)),'[]') FROM (
   SELECT e.original_email,e.normalized_email,b.id AS binding_id,b.project,b.uid,b.active,b.retired_at
   FROM authz.email_identity e LEFT JOIN authz.provider_binding b ON b.identity_id=e.id
   WHERE e.member_id=m.id ORDER BY e.created_at,b.created_at,b.id) q),
  'requests',(SELECT coalesce(jsonb_agg(to_jsonb(q)),'[]') FROM (
   SELECT * FROM authz.family_request r WHERE coalesce(r.member_id,r.conflict_member_id)=m.id
   ORDER BY r.created_at DESC,r.id LIMIT 51 OFFSET p_offset) q),
  'history',(SELECT coalesce(jsonb_agg(to_jsonb(q)),'[]') FROM (
   SELECT * FROM authz.decision d WHERE d.member_id=m.id
   ORDER BY d.occurred_at DESC,d.id LIMIT 51 OFFSET p_offset) q));
END $$;
CREATE FUNCTION authz.owner_decide(p_project text,p_uid text,p_email text,p_operation uuid,
 p_member uuid,p_version bigint,p_action text,p_request uuid,p_binding uuid,p_person text,p_note text,p_verified boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE v_owner uuid; v_actor text; v_payload jsonb; prior authz.owner_operation%ROWTYPE;
 m authz.member%ROWTYPE; v_version bigint;
BEGIN
 v_owner:=authz.owner_require(p_project,p_uid,p_email);
 v_actor:='web-owner:'||v_owner::text;
 IF p_operation IS NULL OR p_member IS NULL OR p_version IS NULL OR p_version<1
 OR p_action IS NULL OR p_action NOT IN ('approve','revoke','reapprove','dismiss-request','link-person','retire-binding')
 OR p_verified IS DISTINCT FROM true OR p_note IS NULL OR length(p_note)>500 OR p_note ~ '[\x01-\x1f\x7f]'
 OR (p_action='dismiss-request')<>(p_request IS NOT NULL)
 OR (p_action='retire-binding')<>(p_binding IS NOT NULL)
 OR (p_action<>'link-person' AND p_person IS NOT NULL)
 THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid decision'; END IF;
 v_payload:=jsonb_build_array(p_member,p_version,p_action,p_request,p_binding,p_person,p_note,p_verified);
 SELECT * INTO prior FROM authz.owner_operation WHERE owner_id=v_owner AND operation_id=p_operation;
 IF FOUND THEN
  IF prior.payload<>v_payload THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='operation key reused'; END IF;
  RETURN jsonb_build_object('outcome','already-applied','version',prior.result_version);
 END IF;
 SELECT * INTO STRICT m FROM authz.member WHERE id=p_member FOR UPDATE;
 IF m.version<>p_version THEN RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='stale decision'; END IF;
 IF p_action='dismiss-request' THEN
  v_version:=authz.dismiss_request(m.id,m.version,p_request,v_actor,p_note);
 ELSE
  IF NOT m.reconciled THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='operator reconciliation required'; END IF;
  CASE p_action
  WHEN 'link-person' THEN
   v_version:=authz.link_person(m.id,m.version,p_person,v_actor,p_note);
  WHEN 'retire-binding' THEN
   UPDATE authz.provider_binding b SET active=false,retired_at=clock_timestamp()
    FROM authz.email_identity e WHERE b.id=p_binding AND b.identity_id=e.id AND e.member_id=m.id AND b.active;
   IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='binding unavailable'; END IF;
   UPDATE authz.member SET version=version+1,updated_at=clock_timestamp() WHERE id=m.id RETURNING version INTO v_version;
   INSERT INTO authz.decision(member_id,action,actor,from_state,to_state,version,note,binding_id)
    VALUES(m.id,p_action,v_actor,m.state,m.state,v_version,p_note,p_binding);
  ELSE
   v_version:=authz.transition(m.id,m.version,p_action,v_actor,p_note);
  END CASE;
 END IF;
 INSERT INTO authz.owner_operation(owner_id,operation_id,payload,result_version) VALUES(v_owner,p_operation,v_payload,v_version);
 RETURN jsonb_build_object('outcome','applied','version',v_version);
END $$;

-- FOS-108 owner decision: preserve request/conflict prose indefinitely.
-- Preserve the old callable interface so scheduled/recovery invocations are harmless.
CREATE OR REPLACE FUNCTION authz.purge_prose() RETURNS integer
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$ SELECT 0 $$;
-- A restored allowlist must never resurrect owner sessions or retired authority.
CREATE OR REPLACE FUNCTION authz.quarantine_restore(p_actor text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE n integer; o record;
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
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
GRANT CREATE ON SCHEMA authz TO genealogy_authz_owner;
DO $$ DECLARE f record; BEGIN
 FOR f IN SELECT p.oid::regprocedure AS signature FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='authz' AND (p.proname LIKE 'owner_%' OR p.proname IN ('purge_prose','quarantine_restore')) LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO genealogy_authz_owner',f.signature);
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
 END LOOP;
END $$;
REVOKE CREATE ON SCHEMA authz FROM genealogy_authz_owner;
GRANT EXECUTE ON FUNCTION authz.owner_add(text,text,text,text,text),authz.owner_retire(uuid,text,text) TO genealogy_authz_operator;
GRANT EXECUTE ON FUNCTION authz.owner_require(text,text,text),authz.owner_queue(text,text,text,boolean,integer),
 authz.owner_detail(text,text,text,uuid,integer),authz.owner_decide(text,text,text,uuid,uuid,bigint,text,uuid,uuid,text,text,boolean)
 TO genealogy_authz_owner_web;
