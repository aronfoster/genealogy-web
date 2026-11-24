from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import AuthConfig
from .database import AuthzConfigurationError, AuthzStore
from .firebase import SESSION_SECONDS, FirebaseVerifier
from .identity import Identity, InvalidIdentity

SESSION_COOKIE = "__Host-family-session"
CSRF_COOKIE = "__Host-family-csrf"


class AuthUnavailable(Exception):
    """A session verification or membership dependency could not be checked."""


class RateLimiter:
    """Bounded per-process fixed windows. Production runs one worker."""

    def __init__(self, window: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.window, self.clock = window, clock
        self.entries: dict[str, tuple[float, int]] = {}
        self.lock = threading.Lock()

    def accept(self, key: str, limit: int) -> bool:
        now = self.clock()
        with self.lock:
            if len(self.entries) >= 10000:
                self.entries = {k: v for k, v in self.entries.items() if v[0] > now}
            expires, count = self.entries.get(key, (now + self.window, 0))
            if expires <= now:
                expires, count = now + self.window, 0
            if count >= limit or (key not in self.entries and len(self.entries) >= 10000):
                return False
            self.entries[key] = (expires, count + 1)
            return True


def local_return(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or "%" in value
        or urlsplit(value).netloc
    ):
        return None
    # Return destinations carry neither callback codes nor arbitrary query text.
    path = urlsplit(value).path
    return (
        None
        if any(segment in {".", ".."} for segment in path.split("/"))
        or path in {"/access", "/api", "/assets"}
        or path.startswith(("/access/", "/api/", "/assets/"))
        else path
    )


class FamilyAuth:
    def __init__(self, config: AuthConfig, *, verifier: Any = None, store: Any = None) -> None:
        self.config = config
        self.verifier = verifier if verifier is not None else FirebaseVerifier(config.project)
        self.store = store if store is not None else AuthzStore(config.dsn)
        self.limiter = RateLimiter(config.rate_window)

    def identity(self, request: Request) -> Identity | None:
        if hasattr(request.state, "family_identity"):
            return request.state.family_identity  # type: ignore[no-any-return]
        cookie = request.cookies.get(SESSION_COOKIE)
        identity = None
        if cookie:
            try:
                if len(cookie) > 16000 or not cookie.isascii():
                    raise InvalidIdentity("Malformed cookie")
                identity = self.verifier.session_identity(cookie)
            except InvalidIdentity:
                # Deny this request without mutating browser cookies: a delayed
                # response must not erase a newer login completed in another tab.
                pass
            except Exception as exc:
                raise AuthUnavailable from exc
        request.state.family_identity = identity
        return identity

    def access_state(self, request: Request) -> str:
        """One verified membership lookup per request, never a cross-request cache."""
        if hasattr(request.state, "family_access_state"):
            return str(request.state.family_access_state)
        identity = self.identity(request)
        try:
            state = self.store.status(identity) if identity is not None else "anonymous"
        except AuthzConfigurationError:
            raise
        except Exception as exc:
            raise AuthUnavailable from exc
        if state not in {"anonymous", "pending", "approved", "revoked"}:
            raise ValueError("Unknown family access state")
        request.state.family_access_state = state
        return str(state)

    def authorize(self, request: Request) -> bool:
        return self.access_state(request) == "approved"

    def _mac(self, text: str) -> str:
        return hmac.new(self.config.csrf_secret.encode(), text.encode(), hashlib.sha256).hexdigest()

    def _csrf(self, request: Request) -> str:
        body = str(int(time.time())) + "." + secrets.token_urlsafe(24)
        return body + "." + self._mac(body + ":" + request.cookies.get(SESSION_COOKIE, ""))

    def _trusted(self, request: Request, *, mutation: bool = False) -> bool:
        origin = request.headers.get("origin")
        if origin and origin != self.config.origin:
            return False
        if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            return False
        if not mutation:
            return True
        if origin != self.config.origin:
            return False
        token = request.headers.get("x-csrf-token", "")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if (
            not token
            or len(token) > 256
            or not token.isascii()
            or not cookie.isascii()
            or not hmac.compare_digest(token, cookie)
        ):
            return False
        return self._valid_csrf(request, token)

    def _valid_csrf(self, request: Request, token: str) -> bool:
        if not token or len(token) > 256 or not token.isascii():
            return False
        try:
            stamp, nonce, signature = token.split(".")
            if not 0 <= time.time() - int(stamp) <= 3600:
                return False
        except ValueError:
            return False
        return hmac.compare_digest(
            signature,
            self._mac(stamp + "." + nonce + ":" + request.cookies.get(SESSION_COOKIE, "")),
        )

    def _ip(self, request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        try:
            trusted = any(
                ipaddress.ip_address(peer) in ipaddress.ip_network(n)
                for n in self.config.proxy_cidrs
            )
            if not trusted:
                return peer
            raw = request.headers.get(self.config.proxy_header, "")
            if not raw or len(raw) > 1024:
                return peer
            chain = [str(ipaddress.ip_address(item.strip())) for item in raw.split(",")]
            if self.config.proxy_header == "cf-connecting-ip":
                return chain[0] if len(chain) == 1 else peer
            for hop in reversed(chain):
                if not any(
                    ipaddress.ip_address(hop) in ipaddress.ip_network(n)
                    for n in self.config.proxy_cidrs
                ):
                    return hop
            return chain[0]
        except ValueError:
            return peer

    def _limit(self, request: Request, action: str, identity: Identity | None = None) -> bool:
        limit = self.config.exchange_limit if action == "exchange" else self.config.request_limit
        raw = repr(identity.key) if identity else self._ip(request)
        if identity is None:
            try:
                address = ipaddress.ip_address(raw)
                if isinstance(address, ipaddress.IPv6Address):
                    # A client must not evade limits by rotating interface addresses.
                    raw = (
                        str(address.ipv4_mapped)
                        if address.ipv4_mapped
                        else str(ipaddress.ip_network((address, 64), strict=False))
                    )
            except ValueError:
                pass
        return self.limiter.accept(
            action + (":identity:" if identity else ":ip:") + self._mac(raw), limit
        )

    @staticmethod
    def _error(status: int) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    400: "Invalid request",
                    401: "Sign in again",
                    403: "Request not permitted",
                    429: "Please try again later",
                    503: "Temporarily unavailable",
                }[status]
            },
            status_code=status,
        )

    def routes(self, render: Callable[..., Response]) -> list[Route]:
        def access(request: Request) -> Response:
            try:
                state = self.access_state(request)
                return render(
                    "access.html",
                    title="Family access",
                    access_state=state,
                    family_access=state == "approved",
                    access_current=True,
                    completion=request.url.path == "/access/complete",
                    return_to=local_return(request.query_params.get("next")) or "",
                )
            except Exception:
                return render(
                    "message.html",
                    title="Family access unavailable",
                    status=503,
                    message="Please try again shortly.",
                )

        def bootstrap(request: Request) -> Response:
            if not self._trusted(request):
                return self._error(403)
            token = request.cookies.get(CSRF_COOKIE, "")
            if not self._valid_csrf(request, token):
                token = self._csrf(request)
            response = JSONResponse(
                {"csrf": token, "firebase": self.config.web, "origin": self.config.origin}
            )
            response.set_cookie(
                CSRF_COOKIE,
                token,
                max_age=3600,
                secure=True,
                httponly=True,
                samesite="lax",
                path="/",
            )
            return response

        def status(request: Request) -> Response:
            if not self._trusted(request):
                return self._error(403)
            try:
                return JSONResponse({"allowed": self.authorize(request)})
            except Exception:
                return self._error(503)

        async def mutation(request: Request) -> Response:
            if not self._trusted(request, mutation=True):
                return self._error(403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return self._error(400)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 20000:
                    return self._error(400)
            try:
                data = json.loads(body)
                if not isinstance(data, dict):
                    return self._error(400)
            except (ValueError, UnicodeError):
                return self._error(400)
            return await run_in_threadpool(process, request, data)

        def process(request: Request, data: dict[str, Any]) -> Response:
            action = request.path_params["action"]
            try:
                if action == "logout":
                    response = JSONResponse({"next": "/access"})
                    response.delete_cookie(
                        SESSION_COOKIE, secure=True, httponly=True, samesite="lax", path="/"
                    )
                    response.delete_cookie(
                        CSRF_COOKIE, secure=True, httponly=True, samesite="lax", path="/"
                    )
                    return response
                if not self._limit(request, action):
                    return self._error(429)
                if action == "exchange":
                    token = data.get("idToken")
                    if not isinstance(token, str) or not token or len(token) > 16000:
                        return self._error(400)
                    identity, cookie = self.verifier.exchange(token)
                    if not self._limit(request, action, identity):
                        return self._error(429)
                    state = self.store.register(identity)
                    response = JSONResponse(
                        {
                            "next": (local_return(data.get("returnTo")) or "/me")
                            if state == "approved"
                            else "/access"
                        }
                    )
                    response.set_cookie(
                        SESSION_COOKIE,
                        cookie,
                        max_age=SESSION_SECONDS,
                        secure=True,
                        httponly=True,
                        samesite="lax",
                        path="/",
                    )
                    return response
                if action == "request":
                    identity = self.identity(request)
                    if identity is None:
                        return self._error(401)
                    if not self._limit(request, action, identity):
                        return self._error(429)
                    explanation = data.get("explanation")
                    if (
                        not isinstance(explanation, str)
                        or len(explanation) > 2000
                        or any((ord(c) < 32 and c != "\n") or ord(c) == 127 for c in explanation)
                    ):
                        return self._error(400)
                    self.store.submit(identity, explanation)
                    return JSONResponse({"submitted": True})
                return self._error(400)
            except InvalidIdentity:
                return self._error(401)
            except Exception:
                return self._error(503)

        return [
            Route("/access", access),
            Route("/access/complete", access),
            Route("/api/access/bootstrap", bootstrap),
            Route("/api/access/status", status),
            Route("/api/access/{action}", mutation, methods=["POST"]),
        ]
