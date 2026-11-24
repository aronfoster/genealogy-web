-- Operational identities are independent of the genealogy corpus and its generations.
-- Legacy rows remain quarantined; they never authorize a session.
ALTER TABLE authz.membership ADD COLUMN quarantine_reason text NOT NULL DEFAULT 'unverified/unmapped legacy subject';
ALTER TABLE authz.access_request ADD COLUMN quarantine_reason text NOT NULL DEFAULT 'unverified legacy request';
REVOKE ALL ON authz.membership,authz.access_request FROM genealogy_app,genealogy_app_rw;
REVOKE SELECT(subject_id,family_access) ON authz.membership FROM genealogy_app,genealogy_app_rw;
REVOKE INSERT(id,email,message) ON authz.access_request FROM genealogy_app_rw;

DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='genealogy_authz_owner') THEN CREATE ROLE genealogy_authz_owner NOLOGIN; END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='genealogy_authz_runtime') THEN CREATE ROLE genealogy_authz_runtime NOLOGIN; END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='genealogy_authz_operator') THEN CREATE ROLE genealogy_authz_operator NOLOGIN; END IF;
END $$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA authz FROM PUBLIC;
GRANT USAGE ON SCHEMA authz TO genealogy_authz_owner,genealogy_authz_runtime,genealogy_authz_operator;
ALTER DEFAULT PRIVILEGES IN SCHEMA authz REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE authz.member (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','approved','revoked')),
 person_id text CHECK (person_id ~ '^per_[0-9a-f-]{36}$'),
 version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
 reconciled boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE authz.email_identity (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 member_id uuid NOT NULL REFERENCES authz.member(id),
 original_email text NOT NULL,
 normalized_email text NOT NULL UNIQUE,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK (octet_length(original_email)=length(original_email) AND length(original_email)<=254),
 CHECK (normalized_email=lower(btrim(original_email) COLLATE "C") AND normalized_email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$')
);
CREATE TABLE authz.provider_binding (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 identity_id uuid NOT NULL REFERENCES authz.email_identity(id),
 project text NOT NULL CHECK (length(project) BETWEEN 1 AND 128),
 uid text NOT NULL CHECK (length(uid) BETWEEN 1 AND 128),
 active boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 retired_at timestamptz,
 CHECK (active=(retired_at IS NULL))
);
CREATE UNIQUE INDEX provider_binding_active ON authz.provider_binding(project,uid) WHERE active;
CREATE TABLE authz.family_request (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 project text NOT NULL, uid text NOT NULL, normalized_email text NOT NULL,
 original_email text NOT NULL,
 verified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 member_id uuid REFERENCES authz.member(id),
 conflict_member_id uuid REFERENCES authz.member(id),
 explanation text CHECK (length(explanation)<=2000 AND explanation !~ '[\x01-\x09\x0b-\x1f\x7f]'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 decided_at timestamptz,
 CHECK (member_id IS NULL OR conflict_member_id IS NULL)
);
CREATE UNIQUE INDEX family_request_open ON authz.family_request(project,uid,normalized_email) WHERE decided_at IS NULL;
CREATE TABLE authz.decision (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), member_id uuid NOT NULL REFERENCES authz.member(id),
 action text NOT NULL, actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),
 from_state text, to_state text, version bigint NOT NULL,
 note text NOT NULL DEFAULT '' CHECK (length(note)<=500),
 binding_id uuid REFERENCES authz.provider_binding(id),
 request_id uuid REFERENCES authz.family_request(id),
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- All writes, including operator writes, go through atomic functions.
GRANT SELECT,INSERT,UPDATE ON authz.member,authz.email_identity,authz.provider_binding,authz.family_request TO genealogy_authz_owner;
GRANT SELECT,INSERT ON authz.decision TO genealogy_authz_owner;
GRANT SELECT ON authz.member,authz.email_identity,authz.provider_binding,authz.family_request,authz.decision,authz.membership,authz.access_request TO genealogy_authz_operator;

CREATE FUNCTION authz.own_status(p_project text,p_uid text,p_email text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
 SELECT coalesce((SELECT CASE WHEN m.reconciled THEN m.state ELSE 'pending' END
 FROM authz.provider_binding b JOIN authz.email_identity e ON e.id=b.identity_id
 JOIN authz.member m ON m.id=e.member_id
 WHERE b.project=p_project AND b.uid=p_uid AND b.active AND e.normalized_email=p_email),'pending')
$$;
CREATE FUNCTION authz.family_allowed(p_project text,p_uid text,p_email text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
 SELECT authz.own_status(p_project,p_uid,p_email)='approved'
$$;

CREATE FUNCTION authz.register_verified(p_project text,p_uid text,p_original text,p_email text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE v_member uuid; v_identity uuid; v_binding uuid; v_conflict uuid;
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
 INSERT INTO authz.member DEFAULT VALUES RETURNING id INTO v_member;
 INSERT INTO authz.email_identity(member_id,original_email,normalized_email)
 VALUES(v_member,p_original,p_email) RETURNING id INTO v_identity;
 INSERT INTO authz.provider_binding(identity_id,project,uid)
 VALUES(v_identity,p_project,p_uid) RETURNING id INTO v_binding;
 INSERT INTO authz.decision(member_id,action,actor,to_state,version,binding_id)
 VALUES(v_member,'register','verified-runtime','pending',1,v_binding);
 RETURN 'pending';
END $$;

CREATE FUNCTION authz.submit_request(p_project text,p_uid text,p_email text,p_explanation text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE v_member uuid; v_status text;
BEGIN
 IF p_explanation IS NULL OR length(p_explanation)>2000 OR p_explanation ~ '[\x01-\x09\x0b-\x1f\x7f]' THEN
 RAISE EXCEPTION 'invalid explanation'; END IF;
 PERFORM pg_advisory_xact_lock(108,1);
 v_status := authz.own_status(p_project,p_uid,p_email);
 SELECT e.member_id INTO v_member FROM authz.provider_binding b
 JOIN authz.email_identity e ON e.id=b.identity_id
 WHERE b.project=p_project AND b.uid=p_uid AND b.active AND e.normalized_email=p_email;
 IF v_member IS NULL THEN
  -- Conflicts must first be registered from a freshly verified identity.
  IF NOT EXISTS(SELECT FROM authz.family_request WHERE project=p_project AND uid=p_uid AND normalized_email=p_email AND decided_at IS NULL) THEN
   RAISE EXCEPTION 'identity not registered';
  END IF;
  UPDATE authz.family_request SET explanation=p_explanation WHERE project=p_project AND uid=p_uid AND normalized_email=p_email AND decided_at IS NULL AND explanation IS NULL;
 ELSE
  IF v_status='approved' THEN RETURN v_status; END IF;
  INSERT INTO authz.family_request(project,uid,normalized_email,original_email,member_id,explanation)
  SELECT p_project,p_uid,p_email,e.original_email,v_member,p_explanation FROM authz.email_identity e
  WHERE e.member_id=v_member AND e.normalized_email=p_email ON CONFLICT DO NOTHING;
 END IF;
 RETURN v_status;
END $$;

CREATE FUNCTION authz.transition(p_member uuid,p_version bigint,p_action text,p_actor text,p_note text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE m authz.member%ROWTYPE; v_next text;
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
 SELECT * INTO STRICT m FROM authz.member WHERE id=p_member FOR UPDATE;
 IF m.version<>p_version THEN RAISE EXCEPTION 'stale member version'; END IF;
 v_next:=CASE
 WHEN p_action='approve' AND m.state='pending' THEN 'approved'
 WHEN p_action='revoke' AND m.state IN ('pending','approved') THEN 'revoked'
 WHEN p_action='reapprove' AND m.state='revoked' THEN 'approved'
 WHEN p_action='reconcile' AND NOT m.reconciled THEN m.state END;
 IF v_next IS NULL THEN RAISE EXCEPTION 'invalid state transition'; END IF;
 UPDATE authz.member SET state=v_next,version=version+1,reconciled=true,updated_at=clock_timestamp() WHERE id=m.id;
 INSERT INTO authz.decision(member_id,action,actor,from_state,to_state,version,note)
 VALUES(m.id,p_action,p_actor,m.state,v_next,m.version+1,p_note);
 IF p_action IN ('approve','revoke','reapprove') THEN
  UPDATE authz.family_request SET decided_at=clock_timestamp() WHERE member_id=m.id AND decided_at IS NULL;
 END IF;
 RETURN m.version+1;
END $$;

CREATE FUNCTION authz.rebind(p_member uuid,p_version bigint,p_request uuid,p_actor text,p_note text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE m authz.member%ROWTYPE; r authz.family_request%ROWTYPE; displaced authz.member%ROWTYPE;
 v_identity uuid; v_binding uuid; v_displaced_binding uuid;
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
 SELECT * INTO STRICT m FROM authz.member WHERE id=p_member FOR UPDATE;
 IF m.version<>p_version THEN RAISE EXCEPTION 'stale member version'; END IF;
 SELECT * INTO STRICT r FROM authz.family_request WHERE id=p_request AND decided_at IS NULL FOR UPDATE;
 IF r.conflict_member_id IS DISTINCT FROM m.id THEN RAISE EXCEPTION 'request does not match member'; END IF;
 IF r.verified_at < clock_timestamp()-interval '24 hours' THEN RAISE EXCEPTION 'fresh sign-in required before rebind'; END IF;
 SELECT id INTO v_identity FROM authz.email_identity WHERE normalized_email=r.normalized_email AND member_id=m.id;
 IF v_identity IS NULL THEN
  INSERT INTO authz.email_identity(member_id,original_email,normalized_email)
  VALUES(m.id,r.original_email,r.normalized_email) RETURNING id INTO v_identity;
 END IF;
 -- Moving a UID from another member retires that binding and audits both members.
 -- Membership state and other bindings of the displaced member remain unchanged.
 SELECT b.id INTO v_displaced_binding FROM authz.provider_binding b
 JOIN authz.email_identity e ON e.id=b.identity_id
 WHERE b.project=r.project AND b.uid=r.uid AND b.active AND e.member_id<>m.id;
 IF FOUND THEN
  SELECT other.* INTO STRICT displaced FROM authz.member other
  JOIN authz.email_identity e ON e.member_id=other.id
  JOIN authz.provider_binding b ON b.identity_id=e.id
  WHERE b.id=v_displaced_binding FOR UPDATE OF other;
  UPDATE authz.provider_binding SET active=false,retired_at=clock_timestamp() WHERE id=v_displaced_binding;
  UPDATE authz.member SET version=version+1,updated_at=clock_timestamp() WHERE id=displaced.id;
  INSERT INTO authz.decision(member_id,action,actor,from_state,to_state,version,note,binding_id,request_id)
  VALUES(displaced.id,'rebind-displaced',p_actor,displaced.state,displaced.state,displaced.version+1,p_note,v_displaced_binding,r.id);
 END IF;
 -- All earlier bindings of this member retire, including an earlier project.
 UPDATE authz.provider_binding b SET active=false,retired_at=clock_timestamp()
 FROM authz.email_identity e WHERE b.identity_id=e.id AND e.member_id=m.id AND b.active;
 INSERT INTO authz.provider_binding(identity_id,project,uid) VALUES(v_identity,r.project,r.uid) RETURNING id INTO v_binding;
 UPDATE authz.member SET version=version+1,updated_at=clock_timestamp() WHERE id=m.id;
 INSERT INTO authz.decision(member_id,action,actor,from_state,to_state,version,note,binding_id,request_id)
 VALUES(m.id,'rebind',p_actor,m.state,m.state,m.version+1,p_note,v_binding,r.id);
 UPDATE authz.family_request SET decided_at=clock_timestamp() WHERE id=r.id;
 RETURN m.version+1;
END $$;

-- Dismissal is a queue decision only, never an approval or binding change.
CREATE FUNCTION authz.dismiss_request(p_member uuid,p_version bigint,p_request uuid,p_actor text,p_note text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE m authz.member%ROWTYPE; r authz.family_request%ROWTYPE;
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
 SELECT * INTO STRICT m FROM authz.member WHERE id=p_member FOR UPDATE;
 IF m.version<>p_version THEN RAISE EXCEPTION 'stale member version'; END IF;
 SELECT * INTO STRICT r FROM authz.family_request WHERE id=p_request AND decided_at IS NULL FOR UPDATE;
 IF coalesce(r.member_id,r.conflict_member_id) IS DISTINCT FROM m.id THEN
  RAISE EXCEPTION 'request does not match member';
 END IF;
 UPDATE authz.family_request SET decided_at=clock_timestamp() WHERE id=r.id;
 UPDATE authz.member SET version=version+1,updated_at=clock_timestamp() WHERE id=m.id;
 INSERT INTO authz.decision(member_id,action,actor,from_state,to_state,version,note,request_id)
 VALUES(m.id,'dismiss-request',p_actor,m.state,m.state,m.version+1,p_note,r.id);
 RETURN m.version+1;
END $$;

CREATE FUNCTION authz.link_person(p_member uuid,p_version bigint,p_person text,p_actor text,p_note text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE m authz.member%ROWTYPE;
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
 SELECT * INTO STRICT m FROM authz.member WHERE id=p_member FOR UPDATE;
 IF m.version<>p_version THEN RAISE EXCEPTION 'stale member version'; END IF;
 UPDATE authz.member SET person_id=p_person,version=version+1,updated_at=clock_timestamp() WHERE id=m.id;
 INSERT INTO authz.decision(member_id,action,actor,from_state,to_state,version,note)
 VALUES(m.id,'link-person',p_actor,m.state,m.state,m.version+1,p_note);
 RETURN m.version+1;
END $$;

CREATE FUNCTION authz.quarantine_restore(p_actor text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE n integer;
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
 WITH changed AS (
 UPDATE authz.member SET reconciled=false,version=version+1,updated_at=clock_timestamp()
 WHERE reconciled RETURNING id,state,version), audited AS (
 INSERT INTO authz.decision(member_id,action,actor,from_state,to_state,version)
 SELECT id,'quarantine-restore',p_actor,state,state,version FROM changed RETURNING id)
 SELECT count(*) INTO n FROM changed;
 RETURN n;
END $$;

CREATE FUNCTION authz.purge_prose() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE n integer;
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
 UPDATE authz.family_request SET explanation=NULL WHERE explanation IS NOT NULL AND
 ((decided_at IS NOT NULL AND decided_at < clock_timestamp()-interval '90 days') OR
 (decided_at IS NULL AND created_at < clock_timestamp()-interval '365 days'));
 GET DIAGNOSTICS n=ROW_COUNT;
 RETURN n;
END $$;

-- Explicit function owners and grants; no PUBLIC execution or schema creation.
GRANT CREATE ON SCHEMA authz TO genealogy_authz_owner;
DO $$ DECLARE f record; BEGIN
 FOR f IN SELECT p.oid::regprocedure AS signature FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='authz' LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO genealogy_authz_owner',f.signature);
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
 END LOOP;
END $$;
REVOKE CREATE ON SCHEMA authz FROM genealogy_authz_owner,genealogy_authz_runtime,genealogy_authz_operator,genealogy_app,genealogy_app_rw;
GRANT EXECUTE ON FUNCTION authz.register_verified(text,text,text,text),authz.own_status(text,text,text),authz.family_allowed(text,text,text),authz.submit_request(text,text,text,text) TO genealogy_authz_runtime;
GRANT EXECUTE ON FUNCTION authz.transition(uuid,bigint,text,text,text),authz.rebind(uuid,bigint,uuid,text,text),authz.dismiss_request(uuid,bigint,uuid,text,text),authz.link_person(uuid,bigint,text,text,text),authz.quarantine_restore(text),authz.purge_prose() TO genealogy_authz_operator;
