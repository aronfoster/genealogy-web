"""Synthetic HTTPS browser harness. Never imported by the production executable."""

from __future__ import annotations

import argparse
import datetime
import io
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import uuid
from pathlib import Path

import psycopg
import uvicorn
from browser_inputs import ScenarioInputs, run_browser
from check_web import serialized_migration
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from genealogy.auth.config import AuthConfig
from genealogy.auth.database import AuthzStore
from genealogy.auth.http import FamilyAuth
from genealogy.auth.identity import Identity, InvalidIdentity
from genealogy.feedback.config import FeedbackConfig
from genealogy.feedback.delivery import FeedbackDelivery
from genealogy.feedback.errors import Issue
from genealogy.feedback.ledger import Ledger
from genealogy.feedback.youtrack import YouTrackClient
from genealogy.importer import import_corpus
from genealogy.input_options import input_options, load_publication_inputs
from genealogy.publication_inputs import strict_json
from genealogy.web.app import create_app
from genealogy.web.repository import PostgresStore

IDENTITY = Identity("synthetic", "browser-user", "browser@example.com", "browser@example.com")
TOKENS = {
    "synthetic-token": IDENTITY,
    "synthetic-google-token": IDENTITY,
    "synthetic-collision-token": Identity(
        "synthetic", "changed-uid", "browser@example.com", "browser@example.com"
    ),
    "synthetic-new-token": Identity("synthetic", "new-user", "new@example.com", "new@example.com"),
    "synthetic-preauth-token": Identity(
        "synthetic", "preauth-user", "preauth@example.com", "preauth@example.com"
    ),
}


class SyntheticVerifier:
    failed = False

    def session_identity(self, cookie: str) -> Identity:
        if self.failed:
            raise OSError("synthetic verifier outage")
        if cookie == "synthetic-cookie":
            return IDENTITY
        if cookie not in TOKENS:
            raise InvalidIdentity("synthetic invalid cookie")
        return TOKENS[cookie]

    def exchange(self, token: str) -> tuple[Identity, str]:
        if token not in TOKENS:
            raise InvalidIdentity("synthetic invalid token")
        return TOKENS[token], "synthetic-cookie" if token == "synthetic-token" else token


class SyntheticFeedback(FeedbackDelivery):
    """Fake REST transport + real disposable receipt ledger. Never shipped to runtime."""

    def __init__(self, dsn: str) -> None:
        self.issues: dict[str, dict[str, object]] = {}
        self.calls = 0
        self.uncertain = False
        self.search_stale = False
        config = FeedbackConfig(
            "perm:synthetic-only", "https://synthetic.youtrack.cloud", "0-123", "0-123"
        )
        super().__init__(YouTrackClient(config, opener=self.open), Ledger(dsn))

    def open(self, request: object, *, timeout: float) -> io.BytesIO:
        from urllib.request import Request

        assert isinstance(request, Request)
        if request.get_method() == "POST":
            assert isinstance(request.data, bytes)
            data = json.loads(request.data)
            key = re.search(r"Submission check: ([0-9a-f-]+)", data["description"])
            assert key is not None
            value = {
                "id": f"2-{len(self.issues) + 1}",
                "idReadable": f"GEN-{len(self.issues) + 1}",
                "project": {"id": "0-123", "shortName": "GEN"},
                "isDraft": False,
                "description": data["description"],
            }
            self.issues[key[1]] = value
            if self.uncertain:
                self.uncertain = False
                self.search_stale = True
                raise OSError("synthetic response loss after provider commit")
            result: object = value
        elif "/api/admin/projects/" in request.full_url:
            result = {"id": "0-123", "shortName": "GEN"}
        else:
            result = [] if self.search_stale else list(self.issues.values())
        return io.BytesIO(json.dumps(result).encode())

    def ensure(self, key: str, title: str, body: str, token: str) -> Issue:
        self.calls += 1
        return super().ensure(key, title, body, token)


def certificate(root: Path) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = root / "cert.pem", root / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    return str(cert_file), str(key_file)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workshop-inputs", dest="inputs", type=Path, required=True)
    args = parser.parse_args()
    input_options(args)
    inputs = load_publication_inputs(args)
    oracle = Path(__file__).parent / "browser_scenarios/auth.json"
    if inputs.profile != "synthetic" or args.corpus is None or args.asset_root is None:
        parser.error("workshop inputs require a synthetic corpus and asset root")
    corpus, assets = args.corpus, args.asset_root
    scenario = ScenarioInputs.load(oracle, "auth")
    cases = strict_json(scenario.raw)
    name = "genealogy_browser_auth_" + uuid.uuid4().hex[:16]
    privileges = ["genealogy_public_ro", "genealogy_family_ro", "genealogy_authz_runtime"]
    roles = [name + "_" + str(i) for i in range(3)]
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as cluster:
        cluster.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        dsn = make_conninfo(os.environ["TEST_DATABASE_URL"], dbname=name)
        try:
            with (
                psycopg.connect(dsn, autocommit=True) as admin,
                tempfile.TemporaryDirectory() as directory,
            ):
                serialized_migration(cluster, admin)
                for role, privilege in zip(roles, privileges, strict=True):
                    admin.execute(
                        sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'synthetic-only'").format(
                            sql.Identifier(role)
                        )
                    )
                    admin.execute(
                        sql.SQL("GRANT {} TO {}").format(
                            sql.Identifier(privilege), sql.Identifier(role)
                        )
                    )
                dsns = [make_conninfo(dsn, user=role, password="synthetic-only") for role in roles]
                import_corpus(
                    corpus,
                    admin,
                    "0" * 40,
                    asset_root=assets,
                    publication_inputs=inputs,
                    build_year=inputs.build_year,
                )
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    base = "https://127.0.0.1:" + str(listener.getsockname()[1])
                    auth_config = AuthConfig(
                        base,
                        "synthetic",
                        "synthetic-key",
                        "synthetic-app",
                        "synthetic.firebaseapp.com",
                        "synthetic-csrf-secret-not-used-outside-test",
                        dsns[2],
                    )
                    verifier = SyntheticVerifier()
                    family = FamilyAuth(auth_config, verifier=verifier, store=AuthzStore(dsns[2]))
                    rate_clock = [0.0]
                    family.limiter.clock = lambda: rate_clock[0]
                    feedback = SyntheticFeedback(dsns[2])
                    app = create_app(
                        store=PostgresStore(dsns[0], dsns[1]), family_auth=family, feedback=feedback
                    )

                    def state(request: Request) -> JSONResponse:
                        # Only this disposable harness has an operator control route.
                        if request.path_params["action"] == "next-rate-window":
                            rate_clock[0] += auth_config.rate_window + 1
                            return JSONResponse({"done": True})
                        if request.path_params["action"] == "feedback-pending":
                            feedback.uncertain = True
                            return JSONResponse({"done": True})
                        if request.path_params["action"] == "feedback-resolve":
                            feedback.search_stale = False
                            return JSONResponse({"done": True})
                        if request.path_params["action"] == "feedback-count":
                            return JSONResponse(
                                {"count": len(feedback.issues), "calls": feedback.calls}
                            )
                        if request.path_params["action"] in {"outage", "recover"}:
                            verifier.failed = request.path_params["action"] == "outage"
                            return JSONResponse({"done": True})
                        if request.path_params["action"] in {
                            "authz-outage",
                            "authz-recover",
                            "public-outage",
                            "public-recover",
                        }:
                            component, action = request.path_params["action"].split("-")
                            role = roles[2] if component == "authz" else roles[0]
                            admin.execute(
                                sql.SQL(
                                    "ALTER ROLE {} "
                                    + ("NOLOGIN" if action == "outage" else "LOGIN")
                                ).format(sql.Identifier(role))
                            )
                            return JSONResponse({"done": True})
                        if request.path_params["action"] == "stage-google":
                            person = cases["senior"]
                            rows = Jsonb(
                                [
                                    {
                                        "email": "preauth@example.com",
                                        "project": "synthetic",
                                        "person_id": person,
                                    }
                                ]
                            )
                            result = admin.execute(
                                "SELECT authz.preauthorization_snapshot(%s)", (rows,)
                            ).fetchone()
                            assert result is not None
                            snapshot = result[0]
                            admin.execute(
                                "SELECT authz.preauthorize_batch(%s,%s,'browser-harness')",
                                (rows, Jsonb(snapshot)),
                            )
                            return JSONResponse({"done": True})
                        row = admin.execute(
                            "SELECT m.id,m.version FROM authz.member m JOIN authz.email_identity e "
                            "ON e.member_id=m.id WHERE e.normalized_email='browser@example.com'"
                        ).fetchone()
                        assert row is not None
                        action = request.path_params["action"]
                        if action in {"link", "unlink"}:
                            person = cases["senior"]
                            admin.execute(
                                "SELECT authz.link_person(%s,%s,%s,'browser-harness','')",
                                (*row, person if action == "link" else None),
                            )
                            return JSONResponse({"done": True, "person": person})
                        if action not in {"approve", "revoke", "reapprove"}:
                            return JSONResponse({}, status_code=400)
                        admin.execute(
                            "SELECT authz.transition(%s,%s,%s,'browser-harness','')", (*row, action)
                        )
                        return JSONResponse({"done": True})

                    app.router.routes.append(Route("/_fixture/{action}", state, methods=["POST"]))
                    cert, key = certificate(Path(directory))
                    server = uvicorn.Server(
                        uvicorn.Config(
                            app,
                            access_log=False,
                            log_level="warning",
                            ssl_certfile=cert,
                            ssl_keyfile=key,
                            proxy_headers=False,
                        )
                    )
                    thread = threading.Thread(
                        target=server.run, kwargs={"sockets": [listener]}, daemon=True
                    )
                    thread.start()
                    try:
                        run_browser(
                            Path(__file__).with_suffix(".mjs"),
                            scenario.raw,
                            {"AUTH_TEST_BASE_URL": base},
                            scenario,
                        )
                    finally:
                        server.should_exit = True
                        thread.join(timeout=10)
        finally:
            cluster.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            for role in roles:
                cluster.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


if __name__ == "__main__":
    main()
