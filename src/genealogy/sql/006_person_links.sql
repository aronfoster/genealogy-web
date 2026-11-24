-- Private operational associations, never part of genealogy publications.
ALTER TABLE authz.decision ADD COLUMN from_person text;
ALTER TABLE authz.decision ADD COLUMN to_person text;

CREATE FUNCTION authz.current_person(p_project text,p_uid text,p_email text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
 SELECT m.person_id
 FROM authz.provider_binding b JOIN authz.email_identity e ON e.id=b.identity_id
 JOIN authz.member m ON m.id=e.member_id
 WHERE b.project=p_project AND b.uid=p_uid AND b.active
 AND e.normalized_email=p_email AND m.state='approved' AND m.reconciled
$$;

CREATE OR REPLACE FUNCTION authz.link_person(p_member uuid,p_version bigint,p_person text,p_actor text,p_note text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authz,pg_temp AS $$
DECLARE m authz.member%ROWTYPE;
BEGIN
 PERFORM pg_advisory_xact_lock(108,1);
 SELECT * INTO STRICT m FROM authz.member WHERE id=p_member FOR UPDATE;
 IF m.version<>p_version THEN RAISE EXCEPTION 'stale member version'; END IF;
 UPDATE authz.member SET person_id=p_person,version=version+1,updated_at=clock_timestamp() WHERE id=m.id;
 INSERT INTO authz.decision(member_id,action,actor,from_state,to_state,version,note,from_person,to_person)
 VALUES(m.id,'link-person',p_actor,m.state,m.state,m.version+1,p_note,m.person_id,p_person);
 RETURN m.version+1;
END $$;

GRANT CREATE ON SCHEMA authz TO genealogy_authz_owner;
ALTER FUNCTION authz.current_person(text,text,text) OWNER TO genealogy_authz_owner;
ALTER FUNCTION authz.link_person(uuid,bigint,text,text,text) OWNER TO genealogy_authz_owner;
REVOKE CREATE ON SCHEMA authz FROM genealogy_authz_owner;
REVOKE ALL ON FUNCTION authz.current_person(text,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION authz.link_person(uuid,bigint,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION authz.current_person(text,text,text) TO genealogy_authz_runtime;
GRANT EXECUTE ON FUNCTION authz.link_person(uuid,bigint,text,text,text) TO genealogy_authz_operator;
