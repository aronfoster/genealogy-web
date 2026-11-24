from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg

from .identity import Identity


class AuthzConfigurationError(RuntimeError):
    """The authorization connection violates its least-privilege contract."""


class AuthzStore:
    """A third DSN, with function-only permissions and no genealogy access."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @contextmanager
    def connection(self) -> Iterator[Any]:
        with psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            row = connection.execute(
                """SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls,
                has_schema_privilege(session_user,'genealogy','USAGE'),
                has_schema_privilege(session_user,'site_family','USAGE'),
                pg_has_role(session_user,'genealogy_authz_operator','MEMBER'),
                pg_has_role(session_user,'genealogy_authz_owner','MEMBER'),
                pg_has_role(session_user,'genealogy_authz_owner_web','MEMBER'),
                has_table_privilege(session_user,'authz.member','INSERT,UPDATE,DELETE'),
                has_table_privilege(session_user,'authz.provider_binding','INSERT,UPDATE,DELETE')
                FROM pg_roles WHERE rolname=session_user"""
            ).fetchone()
            if row is None or any(row):
                raise AuthzConfigurationError("Authz credentials must be least privilege")
            connection.execute("SET LOCAL ROLE genealogy_authz_runtime")
            yield connection

    def register(self, identity: Identity) -> str:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT authz.register_verified(%s,%s,%s,%s)",
                (identity.project, identity.uid, identity.original_email, identity.email),
            ).fetchone()
            return str(row[0])

    def status(self, identity: Identity) -> str:
        with self.connection() as connection:
            return str(
                connection.execute("SELECT authz.own_status(%s,%s,%s)", identity.key).fetchone()[0]
            )

    def allowed(self, identity: Identity) -> bool:
        with self.connection() as connection:
            return bool(
                connection.execute(
                    "SELECT authz.family_allowed(%s,%s,%s)", identity.key
                ).fetchone()[0]
            )

    def current_person(self, identity: Identity) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT authz.current_person(%s,%s,%s)", identity.key
            ).fetchone()
            return str(row[0]) if row[0] is not None else None

    def submit(self, identity: Identity, explanation: str) -> str:
        with self.connection() as connection:
            return str(
                connection.execute(
                    "SELECT authz.submit_request(%s,%s,%s,%s)", (*identity.key, explanation)
                ).fetchone()[0]
            )
