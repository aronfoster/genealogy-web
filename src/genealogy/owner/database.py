from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg

from genealogy.auth.identity import Identity


class OwnerStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @contextmanager
    def connection(self) -> Iterator[Any]:
        with psycopg.connect(self.dsn, connect_timeout=5) as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            unsafe = db.execute("""SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls,
              EXISTS(SELECT FROM pg_roles r WHERE r.rolname <> 'genealogy_authz_owner_web'
               AND r.rolname <> session_user AND pg_has_role(session_user,r.oid,'MEMBER')),
              EXISTS(SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname IN ('authz','genealogy','site_family','site_public','operations','publication')
               AND c.relkind IN ('r','p','v','m') AND (has_table_privilege(session_user,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
               OR has_any_column_privilege(session_user,c.oid,'SELECT,INSERT,UPDATE,REFERENCES'))),
              EXISTS(SELECT FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg_%'
               AND has_schema_privilege(session_user,n.oid,'CREATE')),
              EXISTS(SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
               WHERE n.nspname IN ('authz','genealogy') AND has_function_privilege(session_user,p.oid,'EXECUTE')
               AND p.oid NOT IN ('authz.owner_require(text,text,text)'::regprocedure,
                 'authz.owner_queue(text,text,text,boolean,integer)'::regprocedure,
                 'authz.owner_detail(text,text,text,uuid,integer)'::regprocedure,
                 'authz.owner_decide(text,text,text,uuid,uuid,bigint,text,uuid,uuid,text,text,boolean)'::regprocedure))
              FROM pg_roles WHERE rolname=session_user""").fetchone()
            if unsafe is None or any(unsafe):
                raise RuntimeError("Owner credentials must be least privilege")
            db.execute("SET LOCAL ROLE genealogy_authz_owner_web")
            yield db

    def require(self, identity: Identity) -> None:
        with self.connection() as db:
            db.execute("SELECT authz.owner_require(%s,%s,%s)", identity.key)

    def queue(self, identity: Identity, all_members: bool, offset: int) -> list[dict[str, Any]]:
        with self.connection() as db:
            return list(
                db.execute(
                    "SELECT authz.owner_queue(%s,%s,%s,%s,%s)", (*identity.key, all_members, offset)
                ).fetchone()[0]
            )

    def detail(self, identity: Identity, member: str, offset: int = 0) -> dict[str, Any]:
        with self.connection() as db:
            return dict(
                db.execute(
                    "SELECT authz.owner_detail(%s,%s,%s,%s,%s)", (*identity.key, member, offset)
                ).fetchone()[0]
            )

    def decide(self, identity: Identity, data: dict[str, Any]) -> dict[str, Any]:
        with self.connection() as db:
            return dict(
                db.execute(
                    "SELECT authz.owner_decide(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        *identity.key,
                        *(
                            data.get(k)
                            for k in (
                                "operation_id",
                                "member_id",
                                "expected_version",
                                "action",
                                "request_id",
                                "binding_id",
                                "person_id",
                                "note",
                                "verified",
                            )
                        ),
                    ),
                ).fetchone()[0]
            )
