"""Drive the owner portal and reader against the same disposable membership database."""

import os
import socket
import subprocess
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
import uvicorn
from check_auth_browser import IDENTITY, SyntheticVerifier, certificate
from psycopg import sql
from psycopg.conninfo import make_conninfo

from genealogy.auth.config import AuthConfig
from genealogy.auth.database import AuthzStore
from genealogy.auth.http import FamilyAuth
from genealogy.auth.identity import Identity, InvalidIdentity
from genealogy.owner.app import create_app as create_owner
from genealogy.owner.database import OwnerStore
from genealogy.web.app import create_app
from genealogy.web.repository import PostgresStore

STEWARD = Identity("steward-workshop", "demo-owner", "steward@example.org", "steward@example.org")


class OwnerCredentials:
    def identity(self, token, *, session):
        if token != ("owner-session" if session else "owner-login"):
            raise InvalidIdentity("unknown credential")
        return STEWARD

    def mint(self, token):
        return "owner-session"

    def revoke(self, uid):
        assert uid == STEWARD.uid


@contextmanager
def serve(host, factory, cert, key):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"https://{host}:{listener.getsockname()[1]}"
        server = uvicorn.Server(
            uvicorn.Config(
                factory(origin),
                ssl_certfile=cert,
                ssl_keyfile=key,
                proxy_headers=False,
                access_log=False,
                log_level="error",
            )
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            yield origin
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def test_owner_browser_decisions(database, tmp_path):
    if not os.getenv("OWNER_BROWSER_TEST"):
        pytest.skip("run the installed owner-browser lane after npm ci")
    admin, dsns = database
    people = PostgresStore(*dsns)
    roles = [admin.info.dbname + "_app", admin.info.dbname + "_owner"]
    with ExitStack() as stack:
        for role, privilege in zip(
            roles, ("genealogy_authz_runtime", "genealogy_authz_owner_web"), strict=True
        ):
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'workshop'").format(sql.Identifier(role))
            )
            stack.callback(admin.execute, sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
            admin.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(privilege), sql.Identifier(role))
            )
        app_dsn, owner_dsn = (
            make_conninfo(dsns[0], user=role, password="workshop") for role in roles
        )
        members = AuthzStore(app_dsn)
        members.register(IDENTITY)
        members.submit(IDENTITY, "Workshop request <em>literal markup</em>")
        admin.execute("SELECT authz.owner_add(%s,%s,%s,'workshop','')", STEWARD.key)

        def reader(origin):
            config = AuthConfig(
                origin, IDENTITY.project, "demo", "demo", "demo.example", "r" * 32, app_dsn
            )
            return create_app(
                store=people,
                family_auth=FamilyAuth(config, verifier=SyntheticVerifier(), store=members),
            )

        def owner(origin):
            config = AuthConfig(
                origin, STEWARD.project, "demo", "demo", "demo.example", "s" * 32, owner_dsn
            )
            return create_owner(
                config=config,
                verifier=OwnerCredentials(),
                store=OwnerStore(owner_dsn),
                people=people,
            )

        cert, key = certificate(tmp_path)
        reader_url = stack.enter_context(serve("127.0.0.1", reader, cert, key))
        owner_url = stack.enter_context(serve("localhost", owner, cert, key))
        subprocess.run(
            ["node", str(Path(__file__).with_name("owner_browser.mjs")), owner_url, reader_url],
            check=True,
        )
