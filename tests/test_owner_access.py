"""The installed owner and reader entry points use separate authorization scopes."""

from starlette.testclient import TestClient

from genealogy.auth.config import AuthConfig
from genealogy.auth.identity import Identity, InvalidIdentity
from genealogy.owner.app import CSRF, create_app

ORIGIN = "https://steward.example"


def test_owner_session_requires_current_approval():
    actor = Identity("demo", "keeper", "keeper@example.org", "keeper@example.org")

    class Membership:
        allowed = True

        def require(self, identity):
            if not self.allowed or identity != actor:
                raise InvalidIdentity("denied")

    class Credentials:
        def identity(self, token, *, session):
            if token != ("cookie" if session else "login"):
                raise InvalidIdentity("unknown")
            return actor

        def mint(self, token):
            return "cookie"

    memberships = Membership()
    config = AuthConfig(ORIGIN, "demo", "key", "app", "demo.example", "a" * 32, "synthetic")
    client = TestClient(
        create_app(config=config, verifier=Credentials(), store=memberships, people=object()),
        base_url=ORIGIN,
    )
    assert client.get("/api/status").status_code == 403
    client.get("/api/bootstrap").raise_for_status()
    csrf = client.cookies[CSRF]
    assert (
        client.post(
            "/api/exchange",
            json={"idToken": "login"},
            headers={"origin": "https://reader.example", "x-csrf-token": csrf},
        ).status_code
        == 403
    )
    client.post(
        "/api/exchange", json={"idToken": "login"}, headers={"origin": ORIGIN, "x-csrf-token": csrf}
    ).raise_for_status()
    assert client.get("/api/status").json() == {"owner": True}
    memberships.allowed = False
    denied = client.get("/api/status")
    assert denied.status_code == 403
    assert "no-store" in denied.headers["cache-control"]
