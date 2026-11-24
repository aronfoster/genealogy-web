from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from html import escape
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from firebase_admin import auth
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from genealogy.auth.config import AuthConfig
from genealogy.auth.firebase import FirebaseVerifier
from genealogy.auth.http import RateLimiter
from genealogy.auth.identity import Identity, InvalidIdentity
from genealogy.owner.database import OwnerStore
from genealogy.web.repository import PostgresStore

SESSION = "__Host-owner-session"
CSRF = "__Host-owner-csrf"
SESSION_SECONDS = 3600
ROOT = files("genealogy.owner")


class OwnerVerifier(FirebaseVerifier):
    def identity(self, token: str, *, session: bool) -> Identity:
        claims = self.verify(token, session=session)
        firebase = claims.get("firebase")
        if not isinstance(firebase, dict) or firebase.get("sign_in_provider") != "password":
            raise InvalidIdentity("Invalid verified owner identity")
        stamp = claims.get("auth_time")
        if (
            not isinstance(stamp, (int, float))
            or isinstance(stamp, bool)
            or not 0 <= time.time() - stamp <= (3600 if session else 300)
        ):
            raise InvalidIdentity("Sign in again")
        return Identity.from_claims(claims, self.project)

    def revoke(self, uid: str) -> None:
        auth.revoke_refresh_tokens(uid, app=self.app)

    def mint(self, token: str) -> str:
        return str(
            auth.create_session_cookie(
                token, expires_in=datetime.timedelta(seconds=SESSION_SECONDS), app=self.app
            )
        )


def configuration() -> AuthConfig:
    if any(
        k in os.environ
        for k in (
            "FIREBASE_AUTH_EMULATOR_HOST",
            "FIREBASE_EMULATOR_HUB",
            "GENEALOGY_AUTH_BYPASS",
            "GENEALOGY_DEV_AUTH",
        )
    ):
        raise ValueError("Emulators and bypasses are not supported")
    web = json.loads(os.environ.get("GENEALOGY_OWNER_FIREBASE_WEB_CONFIG", "{}"))
    result = AuthConfig(
        origin=os.environ.get("GENEALOGY_OWNER_ORIGIN", ""),
        project=web.get("projectId", ""),
        api_key=web.get("apiKey", ""),
        app_id=web.get("appId", ""),
        auth_domain=web.get("authDomain", ""),
        csrf_secret=os.environ.get("GENEALOGY_OWNER_CSRF_SECRET", ""),
        dsn=os.environ.get("GENEALOGY_OWNER_DSN", ""),
    )
    reader_origin = os.environ.get("GENEALOGY_ORIGIN", "")
    reader_project = os.environ.get("GENEALOGY_READER_FIREBASE_PROJECT", "")
    if (
        not reader_origin
        or not reader_project
        or result.origin == reader_origin
        or result.project == reader_project
    ):
        raise ValueError("Owner requires a separate origin and Firebase project")
    if not os.environ.get("GENEALOGY_FAMILY_DSN"):
        raise ValueError("Independent family reader required for person selection")
    return result


def create_app(
    *, config: AuthConfig | None = None, verifier: Any = None, store: Any = None, people: Any = None
) -> Starlette:
    config = config or configuration()
    verifier = verifier if verifier is not None else OwnerVerifier(config.project)
    store = store if store is not None else OwnerStore(config.dsn)
    people = people if people is not None else PostgresStore("", os.environ["GENEALOGY_FAMILY_DSN"])
    site_name = os.environ.get("GENEALOGY_SITE_NAME", "").strip() or "Family archive"
    shell = (
        ROOT.joinpath("index.html")
        .read_text()
        .replace("{{ site_name }}", escape(site_name.upper()))
    )
    limiter = RateLimiter(600)

    def mac(value: str) -> str:
        return hmac.new(config.csrf_secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def signed(value: str, purpose: str) -> str:
        return value + "." + mac(purpose + ":" + value)

    def unsign(value: str, purpose: str) -> str:
        if not value or len(value) > 20000 or not value.isascii():
            raise InvalidIdentity("Invalid credentials")
        body, _, signature = value.rpartition(".")
        if not hmac.compare_digest(signature, mac(purpose + ":" + body)):
            raise InvalidIdentity("Invalid credentials")
        return body

    def owner(request: Request) -> Identity:
        token = unsign(request.cookies.get(SESSION, ""), "session")
        identity: Identity = verifier.identity(token, session=True)
        store.require(identity)
        return identity

    def csrf(request: Request) -> str:
        return signed(
            str(int(time.time())) + ":" + secrets.token_urlsafe(24),
            "csrf:" + request.cookies.get(SESSION, ""),
        )

    def trusted(request: Request) -> bool:
        if request.headers.get("host") != urlsplit(config.origin).netloc:
            return False
        # Email/notification links arrive as cross-site top-level navigations.
        # Only the generic shell is public; it contains no owner or request data.
        if (
            request.method == "GET"
            and request.url.path == "/"
            and request.headers.get("sec-fetch-mode") == "navigate"
        ):
            return True
        if request.headers.get("origin") not in (None, config.origin):
            return False
        if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            return False
        if request.method != "POST":
            return True
        token = request.headers.get("x-csrf-token", "")
        if (
            request.headers.get("origin") != config.origin
            or token != request.cookies.get(CSRF)
            or len(token) > 256
        ):
            return False
        try:
            value = unsign(token, "csrf:" + request.cookies.get(SESSION, ""))
            return 0 <= time.time() - int(value.split(":", 1)[0]) <= 3600
        except (InvalidIdentity, ValueError):
            return False

    def error(code: int) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    400: "Invalid request",
                    401: "Sign in again",
                    403: "Request not permitted",
                    404: "Not found",
                    409: "Decision changed. Review the current record.",
                    429: "Please try again later",
                    503: "Temporarily unavailable",
                }[code]
            },
            status_code=code,
        )

    def offset(request: Request) -> int:
        value = int(request.query_params.get("offset", "0"))
        if not 0 <= value <= 1000000:
            raise ValueError("Invalid page")
        return value

    def handle(request: Request, data: dict[str, Any]) -> Response:
        path = request.url.path
        if path == "/":
            return Response(shell, media_type="text/html")
        if path in ("/owner.js", "/owner.css"):
            return Response(
                ROOT.joinpath(path[1:]).read_text(),
                media_type="text/javascript" if path.endswith(".js") else "text/css",
            )
        if path == "/internal/ready":
            # nginx denies this exact route; only the protected Unix socket exposes it.
            with store.connection(), people.open("family") as repository:
                repository.search("readiness", 0)
            return JSONResponse({"ready": True})
        if path == "/api/bootstrap":
            response = JSONResponse({"firebase": config.web})
            response.set_cookie(
                CSRF,
                csrf(request),
                secure=True,
                httponly=False,
                samesite="strict",
                max_age=3600,
                path="/",
            )
            return response
        if path == "/api/logout":
            if request.cookies.get(SESSION):
                try:
                    identity = owner(request)
                except (InvalidIdentity, psycopg.errors.InsufficientPrivilege):
                    pass  # Already invalid; clear local state as well.
                else:
                    verifier.revoke(identity.uid)
            response = JSONResponse({"signed_out": True})
            response.delete_cookie(SESSION, secure=True, httponly=True, samesite="strict", path="/")
            response.delete_cookie(CSRF, secure=True, samesite="strict", path="/")
            return response
        if path == "/api/exchange":
            if (
                set(data) != {"idToken"}
                or not isinstance(data["idToken"], str)
                or not 1 <= len(data["idToken"]) <= 16000
            ):
                return error(400)
            identity = verifier.identity(data["idToken"], session=False)
            store.require(identity)  # No cookie is minted for an ordinary verified user.
            cookie = verifier.mint(data["idToken"])
            response = JSONResponse({"signed_in": True})
            response.set_cookie(
                SESSION,
                signed(cookie, "session"),
                secure=True,
                httponly=True,
                samesite="strict",
                max_age=SESSION_SECONDS,
                path="/",
            )
            response.delete_cookie(CSRF, secure=True, samesite="strict", path="/")
            return response
        identity = owner(request)  # Every private read and every mutation, uncached.
        if path == "/api/status":
            return JSONResponse({"owner": True})
        if path == "/api/members":
            return JSONResponse(
                {
                    "members": store.queue(
                        identity, request.query_params.get("all") == "1", offset(request)
                    )
                }
            )
        if path == "/api/member":
            member = str(UUID(request.query_params.get("id", "")))
            try:
                return JSONResponse(store.detail(identity, member, offset(request)))
            except psycopg.errors.NoDataFound:
                return error(404)
        if path == "/api/people":
            query = request.query_params.get("q", "").strip()
            if not 1 <= len(query) <= 100:
                return error(400)
            with people.open("family") as repository:
                rows = repository.search(query, offset(request))
                options = []
                for person in rows:
                    claim = repository.get(
                        person.get("display_name_assertion_id", ""), ["assertions"]
                    )
                    options.append(
                        {
                            "id": person["id"],
                            "name": claim["value"]["raw"] if claim else "Name not yet established",
                        }
                    )
            return JSONResponse({"people": options})
        if path == "/api/decide":
            required = {
                "operation_id",
                "member_id",
                "expected_version",
                "action",
                "note",
                "verified",
            }
            if not required <= data.keys() or data.keys() - required - {
                "request_id",
                "binding_id",
                "person_id",
            }:
                return error(400)
            if (
                type(data["expected_version"]) is not int
                or not 1 <= data["expected_version"] < 2**63
                or data["verified"] is not True
            ):
                return error(400)
            if (
                not isinstance(data["note"], str)
                or len(data["note"]) > 500
                or any(ord(c) < 32 or ord(c) == 127 for c in data["note"])
            ):
                return error(400)
            if data["action"] not in (
                "approve",
                "revoke",
                "reapprove",
                "dismiss-request",
                "link-person",
                "retire-binding",
            ):
                return error(400)
            for field in ("operation_id", "member_id", "request_id", "binding_id"):
                if data.get(field) is not None:
                    data[field] = str(UUID(data[field]))
            if data.get("person_id") is not None:
                if data["action"] != "link-person" or not isinstance(data["person_id"], str):
                    return error(400)
                with people.open("family") as repository:
                    if repository.get(data["person_id"], ["people"]) is None:
                        return error(400)
            result = store.decide(identity, data)
            # Result receipt is historical; clients must read current state even on retry.
            return JSONResponse(result)
        return error(404)

    async def endpoint(request: Request) -> Response:
        try:
            if not trusted(request):
                return error(403)
            data: dict[str, Any] = {}
            if request.method == "POST":
                if request.headers.get("content-type", "").split(";")[0] != "application/json":
                    return error(400)
                chunks = bytearray()
                async for chunk in request.stream():
                    chunks.extend(chunk)
                    if len(chunks) > 20000:
                        return error(400)
                parsed = json.loads(chunks)
                if not isinstance(parsed, dict):
                    return error(400)
                data = parsed
                # One worker on a proxy-only socket: global exchange ceiling also
                # bounds callers that vary cookies or spoof forwarding headers.
                key = (
                    "exchange"
                    if request.url.path == "/api/exchange"
                    else mac(request.cookies.get(SESSION, ""))
                )
                if not limiter.accept(key, 20 if key == "exchange" else 120):
                    return error(429)
            return await run_in_threadpool(handle, request, data)
        except (InvalidIdentity, psycopg.errors.InsufficientPrivilege):
            return error(403)
        except (
            ValueError,
            TypeError,
            KeyError,
            psycopg.errors.InvalidParameterValue,
            psycopg.errors.InvalidTextRepresentation,
        ):
            return error(400)
        except (
            psycopg.errors.SerializationFailure,
            psycopg.errors.RaiseException,
            psycopg.errors.NoDataFound,
        ):
            return error(409)
        except Exception:
            # Provider/DB exceptions can contain emails, prose, tokens or DSNs.
            return error(503)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        yield
        if hasattr(verifier, "close"):
            verifier.close()

    app = Starlette(
        routes=[
            Route(p, endpoint, methods=[method])
            for p, method in (
                ("/", "GET"),
                ("/owner.js", "GET"),
                ("/owner.css", "GET"),
                ("/api/bootstrap", "GET"),
                ("/internal/ready", "GET"),
                ("/api/status", "GET"),
                ("/api/members", "GET"),
                ("/api/member", "GET"),
                ("/api/people", "GET"),
                ("/api/exchange", "POST"),
                ("/api/logout", "POST"),
                ("/api/decide", "POST"),
            )
        ],
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Vary": "Cookie",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Cross-Origin-Opener-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            }
        )
        return response

    return app
