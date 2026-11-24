-- FOS-182: 007 is reserved for the independent owner portal. No old SQL changes.
-- Staged decisions share the existing member/version/audit/revocation/quarantine
-- machinery, but are NOT email identities and never contain manufactured UIDs.
CREATE TABLE authz.preauthorization (
 normalized_email text PRIMARY KEY CHECK (
  octet_length(normalized_email)=length(normalized_email) AND length(normalized_email)<=254
  AND normalized_email=lower(btrim(normalized_email) COLLATE "C")
  AND normalized_email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'),
 project text NOT NULL CHECK (length(project) BETWEEN 1 AND 128),
 person_id text NOT NULL CHECK (person_id ~ '^per_[0-9a-f-]{36}$'),
 member_id uuid NOT NULL UNIQUE REFERENCES authz.member(id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 claimed_at timestamptz
);
GRANT SELECT,INSERT,UPDATE ON authz.preauthorization TO genealogy_authz_owner;
GRANT SELECT ON authz.preauthorization TO genealogy_authz_operator;

-- Exact review state, restricted to the operator. Member versions capture all
-- decisions, bindings, mapping changes and restore quarantine, including ABA.
CREATE FUNCTION authz.preauthorization_snapshot(p_entries jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 IF jsonb_typeof(p_entries) IS DISTINCT FROM 'array' OR jsonb_array_length(p_entries) NOT BETWEEN 1 AND 1000
 THEN RAISE EXCEPTION 'invalid enrollment batch'; END IF;
 PERFORM pg_advisory_xact_lock(108,1);
 SELECT jsonb_agg(jsonb_build_object('email',r->>'email','preauthorization',to_jsonb(p),
  'member',to_jsonb(m),'identity',to_jsonb(e), 'request_seen',
  EXISTS(SELECT FROM authz.family_request q WHERE q.normalized_email=r->>'email')) ORDER BY r->>'email') INTO result
 FROM jsonb_array_elements(p_entries) r
 LEFT JOIN authz.preauthorization p ON p.normalized_email=r->>'email'
 LEFT JOIN authz.member m ON m.id=p.member_id
 LEFT JOIN authz.email_identity e ON e.normalized_email=r->>'email';
 RETURN result;
END $$;

-- Whole batch commits or fails. Caller verifies the current family publication
-- through its separate reader; operator and runtime gain no corpus privileges.
CREATE FUNCTION authz.preauthorize_batch(p_entries jsonb,p_expected jsonb,p_actor text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE r jsonb; pre authz.preauthorization%ROWTYPE; m authz.member%ROWTYPE;
 v_member uuid; n integer:=0;
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
 IF p_actor IS NULL OR length(p_actor) NOT BETWEEN 1 AND 200 OR p_actor ~ '[[:cntrl:]]'
 THEN RAISE EXCEPTION 'invalid actor'; END IF;
 IF authz.preauthorization_snapshot(p_entries) IS DISTINCT FROM p_expected
 THEN RAISE EXCEPTION 'stale enrollment review'; END IF;
 IF (SELECT count(*)<>count(DISTINCT item->>'email') FROM jsonb_array_elements(p_entries) item)
 THEN RAISE EXCEPTION 'duplicate enrollment email'; END IF;
 FOR r IN SELECT value FROM jsonb_array_elements(p_entries) LOOP
  IF jsonb_typeof(r) IS DISTINCT FROM 'object' OR NOT (r ?& ARRAY['email','project','person_id'])
   OR (r - ARRAY['email','project','person_id']) <> '{}'::jsonb
   OR jsonb_typeof(r->'email') IS DISTINCT FROM 'string'
   OR jsonb_typeof(r->'project') IS DISTINCT FROM 'string'
   OR jsonb_typeof(r->'person_id') IS DISTINCT FROM 'string'
  THEN RAISE EXCEPTION 'invalid enrollment row'; END IF;
  SELECT * INTO pre FROM authz.preauthorization WHERE normalized_email=r->>'email';
  IF FOUND THEN
   SELECT * INTO STRICT m FROM authz.member WHERE id=pre.member_id;
   IF pre.project IS DISTINCT FROM r->>'project' OR pre.person_id IS DISTINCT FROM r->>'person_id'
    OR m.person_id IS DISTINCT FROM pre.person_id OR m.state<>'approved' OR NOT m.reconciled
   THEN RAISE EXCEPTION 'existing enrollment decision conflicts'; END IF;
   -- Exact retry is a no-op, never a reapproval, relink or fresh audit event.
   CONTINUE;
  END IF;
  IF EXISTS(SELECT FROM authz.email_identity WHERE normalized_email=r->>'email')
   OR EXISTS(SELECT FROM authz.family_request WHERE normalized_email=r->>'email')
  THEN RAISE EXCEPTION 'existing identity requires attended review'; END IF;
  INSERT INTO authz.member(state,person_id) VALUES('approved',r->>'person_id') RETURNING id INTO v_member;
  INSERT INTO authz.preauthorization(normalized_email,project,person_id,member_id)
  VALUES(r->>'email',r->>'project',r->>'person_id',v_member);
  INSERT INTO authz.decision(member_id,action,actor,to_state,version,to_person)
  VALUES(v_member,'preauthorize',p_actor,'approved',1,r->>'person_id');
  n:=n+1;
 END LOOP;
 RETURN n;
END $$;

CREATE OR REPLACE FUNCTION authz.register_verified(p_project text,p_uid text,p_original text,p_email text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE v_member uuid; v_identity uuid; v_binding uuid; v_conflict uuid;
 pre authz.preauthorization%ROWTYPE; m authz.member%ROWTYPE;
BEGIN
 IF length(p_project) NOT BETWEEN 1 AND 128 OR length(p_uid) NOT BETWEEN 1 AND 128
 OR p_project IS NULL OR p_uid IS NULL OR p_original IS NULL OR p_email IS NULL
 OR octet_length(p_original)<>length(p_original) OR length(p_original)>254
 OR p_email<>lower(btrim(p_original) COLLATE "C") OR p_email !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'
 THEN RAISE EXCEPTION 'invalid verified identity'; END IF;
 -- Family-scale serialization makes registration and operator rebinding deterministic.
 PERFORM pg_advisory_xact_lock(108,1);
 SELECT e.member_id INTO v_member FROM authz.provider_binding b
 JOIN authz.email_identity e ON e.id=b.identity_id
 WHERE b.project=p_project AND b.uid=p_uid AND b.active AND e.normalized_email=p_email;
 IF FOUND THEN RETURN authz.own_status(p_project,p_uid,p_email); END IF;
 SELECT member_id INTO v_conflict FROM authz.email_identity WHERE normalized_email=p_email;
 IF v_conflict IS NULL THEN
  SELECT e.member_id INTO v_conflict FROM authz.provider_binding b
  JOIN authz.email_identity e ON e.id=b.identity_id WHERE b.project=p_project AND b.uid=p_uid;
 END IF;
 IF v_conflict IS NOT NULL THEN
  INSERT INTO authz.family_request(project,uid,normalized_email,original_email,conflict_member_id)
  VALUES(p_project,p_uid,p_email,p_original,v_conflict)
  ON CONFLICT (project,uid,normalized_email) WHERE decided_at IS NULL
  DO UPDATE SET verified_at=clock_timestamp();
  RETURN 'pending';
 END IF;
 -- A staged approval is not a provider identity. Only this verified-runtime
 -- boundary may create its first real binding, under the same identity lock.
 SELECT * INTO pre FROM authz.preauthorization WHERE normalized_email=p_email;
 IF FOUND THEN
  SELECT * INTO STRICT m FROM authz.member WHERE id=pre.member_id FOR UPDATE;
  IF pre.project<>p_project OR pre.claimed_at IS NOT NULL OR NOT m.reconciled
   OR m.state<>'approved' OR m.person_id IS DISTINCT FROM pre.person_id THEN
   -- Keep the verified attempt reviewable without changing the staged decision
   -- or inventing a binding. Repeated sign-ins refresh this one open request.
   INSERT INTO authz.family_request(project,uid,normalized_email,original_email,conflict_member_id)
   VALUES(p_project,p_uid,p_email,p_original,m.id)
   ON CONFLICT (project,uid,normalized_email) WHERE decided_at IS NULL
   DO UPDATE SET verified_at=clock_timestamp();
   RETURN 'pending';
  END IF;
  INSERT INTO authz.email_identity(member_id,original_email,normalized_email)
  VALUES(m.id,p_original,p_email) RETURNING id INTO v_identity;
  INSERT INTO authz.provider_binding(identity_id,project,uid)
  VALUES(v_identity,p_project,p_uid) RETURNING id INTO v_binding;
  UPDATE authz.preauthorization SET claimed_at=clock_timestamp() WHERE member_id=m.id;
  UPDATE authz.member SET version=version+1,updated_at=clock_timestamp() WHERE id=m.id;
  INSERT INTO authz.decision(member_id,action,actor,from_state,to_state,version,binding_id,from_person,to_person)
  VALUES(m.id,'claim-preauthorization','verified-runtime',m.state,m.state,m.version+1,v_binding,m.person_id,m.person_id);
  RETURN authz.own_status(p_project,p_uid,p_email);
 END IF;
 INSERT INTO authz.member DEFAULT VALUES RETURNING id INTO v_member;
 INSERT INTO authz.email_identity(member_id,original_email,normalized_email)
 VALUES(v_member,p_original,p_email) RETURNING id INTO v_identity;
 INSERT INTO authz.provider_binding(identity_id,project,uid)
 VALUES(v_identity,p_project,p_uid) RETURNING id INTO v_binding;
 INSERT INTO authz.decision(member_id,action,actor,to_state,version,binding_id)
 VALUES(v_member,'register','verified-runtime','pending',1,v_binding);
 RETURN 'pending';
END $$;

GRANT CREATE ON SCHEMA authz TO genealogy_authz_owner;
ALTER FUNCTION authz.preauthorization_snapshot(jsonb) OWNER TO genealogy_authz_owner;
ALTER FUNCTION authz.preauthorize_batch(jsonb,jsonb,text) OWNER TO genealogy_authz_owner;
REVOKE CREATE ON SCHEMA authz FROM genealogy_authz_owner;
REVOKE ALL ON FUNCTION authz.preauthorization_snapshot(jsonb),authz.preauthorize_batch(jsonb,jsonb,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION authz.preauthorization_snapshot(jsonb),authz.preauthorize_batch(jsonb,jsonb,text) TO genealogy_authz_operator;
