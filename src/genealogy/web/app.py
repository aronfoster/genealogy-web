"""ASGI factory for public and authenticated archive readers."""

from __future__ import annotations

import hashlib
import logging
import os
import traceback
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import uvicorn
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from genealogy.auth.config import AuthConfig
from genealogy.auth.database import AuthzConfigurationError
from genealogy.auth.http import AuthUnavailable, FamilyAuth, local_return
from genealogy.feedback.config import FeedbackConfig
from genealogy.feedback.delivery import FeedbackDelivery
from genealogy.feedback.http import routes as feedback_routes
from genealogy.feedback.ledger import Ledger
from genealogy.feedback.youtrack import YouTrackClient
from genealogy.portrait import MAX_BYTES, safe_png

from .errors import NotFound
from .presentation import Presenter
from .repository import Audience, PostgresStore, Record
from .source_filters import SOURCE_KIND_LABELS, SOURCE_KINDS
from .urls import record_url

FamilyAuthorizer = Callable[[Request], bool]
AssetURL = Callable[[Record, Audience], str | None]
ROOT = files("genealogy.web")
LOGGER = logging.getLogger(__name__)
TEMPLATES = Environment(
    loader=FileSystemLoader(str(ROOT.joinpath("templates"))), autoescape=select_autoescape(["html"])
)


TEMPLATES.globals.update(site_name="Family archive", admin_contact="the archive administrator")


def render(template: str, *, status: int = 200, **context: Any) -> HTMLResponse:
    if status >= 400:
        # Errors cannot claim an access state, even after earlier authorization.
        context.update(static_error=True, family_access=False, access_state=None)
    return HTMLResponse(TEMPLATES.get_template(template).render(**context), status_code=status)


def create_app(
    *,
    store: Any = None,
    authorize_family: FamilyAuthorizer | None = None,
    asset_url: AssetURL | None = None,
    portrait_root: Path | None = None,
    family_auth: FamilyAuth | None = None,
    feedback: Any = None,
) -> Starlette:
    """Trusted in-process hooks, never HTTP headers/query parameters as authority.

    authorize_family must verify credentials AND current approved membership on
    EVERY request. Readers without authorization see only public records. asset_url
    must return a short-lived HTTPS URL for the supplied authorized asset only.
    """
    if portrait_root is None and os.environ.get("GENEALOGY_PORTRAIT_ROOT"):
        portrait_root = Path(os.environ["GENEALOGY_PORTRAIT_ROOT"])
    if store is None:
        auth_config = AuthConfig.from_env()
        feedback_config = FeedbackConfig.from_env()
        if feedback_config and feedback is None:
            if not auth_config:
                raise ValueError("Feedback requires approved family authentication")
            feedback = FeedbackDelivery(YouTrackClient(feedback_config), Ledger(auth_config.dsn))
        public_dsn = os.environ.get("GENEALOGY_PUBLIC_DSN", "")
        if not public_dsn.strip():
            raise RuntimeError("GENEALOGY_PUBLIC_DSN must be configured")
        store = PostgresStore(public_dsn, os.environ.get("GENEALOGY_FAMILY_DSN"))
        if auth_config:
            if authorize_family is not None:
                raise RuntimeError("Production auth cannot be replaced by a callback")
            family_auth = FamilyAuth(auth_config)
    if family_auth:
        if authorize_family is not None:
            raise RuntimeError("Choose one family authenticator")
        authorize_family = family_auth.authorize

    site_name = os.environ.get("GENEALOGY_SITE_NAME", "").strip() or "Family archive"
    admin_contact = (
        os.environ.get("GENEALOGY_ADMIN_CONTACT", "").strip() or "the archive administrator"
    )

    def render_page(template: str, **context: Any) -> HTMLResponse:
        # Per-app context avoids leaking enablement between injected test apps.
        return render(
            template,
            site_name=site_name,
            admin_contact=admin_contact,
            feedback_enabled=bool(family_auth and feedback),
            **context,
        )

    def access_state(request: Request, approved: bool) -> str | None:
        if getattr(request.state, "public_fallback", False):
            return "public-only"
        if family_auth:
            return family_auth.access_state(request)
        # Trusted in-process test authorizers can still select the family audience.
        return "approved" if approved else None

    def document_audience(request: Request) -> Audience:
        # This opt-out only removes access. It lets a failed browser status check
        # replace its private DOM even if document authorization still succeeds.
        # It is never consulted by APIs, asset delivery or account routes.
        if request.query_params.get("public") == "1":
            request.state.public_fallback = True
            return "public"
        try:
            return "family" if authorize_family and authorize_family(request) else "public"
        except AuthUnavailable as exc:
            # Only the auth boundary can raise this. Reader/presenter failures
            # below are not retried, and no previous authorization is reused.
            LOGGER.warning(
                "Auth unavailable; serving public documents: %s",
                type(exc.__cause__ or exc).__name__,
            )
            request.state.public_fallback = True
            return "public"

    def not_found(request: Request, exc: Exception) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "Not found"}, status_code=404)
        return render_page(
            "message.html",
            status=404,
            title="Page not found",
            message="This page is not available. You can return to the homepage.",
        )

    def unavailable(request: Request, exc: Exception) -> Response:
        # Exception messages/locals/SQL and formatted tracebacks can contain secrets.
        # Retain the class and application frame locations, never request data.
        cause = exc.__cause__ if isinstance(exc, AuthUnavailable) and exc.__cause__ else exc
        frames = [
            f"{frame.f_globals.get('__name__')}.{frame.f_code.co_name}:{line}"
            for frame, line in traceback.walk_tb(cause.__traceback__)
            if str(frame.f_globals.get("__name__", "")).startswith("genealogy.")
        ]
        LOGGER.error(
            "Archive request failed: %s; cause=%s; frames=%s",
            type(exc).__name__,
            type(cause).__name__,
            " > ".join(frames),
        )
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "Archive temporarily unavailable"}, status_code=503)
        return render_page(
            "message.html",
            status=503,
            title="Archive temporarily unavailable",
            message="Please try again shortly.",
        )

    def page(request: Request) -> Response:
        api = request.url.path.startswith("/api/")
        route = request.path_params.get("view", "home")
        rid = request.path_params.get("record_id", "")
        q = request.query_params.get("q", "").strip()
        kind = request.query_params.get("kind", "") if route == "sources" else ""
        scope = request.query_params.get("scope", "tree") if route in {"home", "people"} else "tree"
        offset_text = request.query_params.get("offset", "0")
        if (
            len(q) > 200
            or (kind and kind not in SOURCE_KINDS)
            or scope not in {"tree", "all"}
            or not offset_text.isascii()
            or not offset_text.isdigit()
            or len(offset_text) > 6
        ):
            return (
                JSONResponse({"error": "Invalid search"}, status_code=400)
                if api
                else render_page(
                    "message.html",
                    status=400,
                    title="Invalid search",
                    message="Use a shorter search and a valid page.",
                )
            )
        offset = int(offset_text)
        try:
            audience: Audience = (
                ("family" if authorize_family and authorize_family(request) else "public")
                if api or route in {"asset", "portrait"}
                else document_audience(request)
            )
            with store.open(audience) as repository:
                presenter = Presenter(repository, hosted_copies_enabled=bool(asset_url))
                data: Record
                if route in {"home", "people"}:
                    rows = repository.search(q, offset, scope)
                    data = {
                        "people": [presenter.browsing_card(r["id"], q) for r in rows[:25]],
                        "query": q,
                        "scope": scope,
                        "next": offset + 25 if len(rows) > 25 else None,
                        "previous": max(0, offset - 25) if offset else None,
                    }
                    title = "Our family, in the records" if route == "home" else "Find a person"
                elif route in {"person", "persona"}:
                    category = "people" if route == "person" else "personas"
                    row = repository.get(rid, [category])
                    if not row:
                        raise NotFound
                    data = presenter.profile(rid)
                    if "favorite_color" in row:
                        data["favorite_color"] = row["favorite_color"]
                    if "profile_photo" in row and portrait_root:
                        aid = row["profile_photo"]["asset_id"]
                        derivative = repository.get(aid, ["assets"])
                        if (
                            derivative
                            and derivative.get("portrait_attribution")
                            and derivative.get("bytes_available")
                        ):
                            data["profile_photo"] = {
                                "url": f"/portraits/{audience}/{aid}",
                                "attribution": derivative["portrait_attribution"],
                            }
                    title = data["name"]
                elif route == "family":
                    data = presenter.family(rid)
                    title = "Family of " + data["focus"]["name"]
                elif route == "assertion":
                    claim = repository.get(rid, ["assertions", "alternatives", "history"])
                    if claim is None:
                        raise NotFound
                    data = presenter.claim(claim)
                    title = data["label"]
                elif route == "sources":
                    rows = repository.sources(q, kind, offset)
                    counts = {r["value"]: r["count"] for r in repository.source_kind_counts(q)}
                    all_total = sum(counts.values())
                    if kind:
                        counts.setdefault(kind, 0)
                    kinds = sorted(
                        [
                            {"value": value, "label": SOURCE_KIND_LABELS[value], "count": count}
                            for value, count in counts.items()
                        ],
                        key=lambda item: (-item["count"], item["label"], item["value"]),
                    )
                    data = {
                        "sources": [presenter.source_card(r) for r in rows[:25]],
                        "query": q,
                        "kind": kind,
                        "kind_label": SOURCE_KIND_LABELS.get(kind, "All types"),
                        "total": counts[kind] if kind else all_total,
                        "all_total": all_total,
                        "kinds": kinds,
                        "next": offset + 25 if len(rows) > 25 else None,
                        "previous": max(0, offset - 25) if offset else None,
                    }
                    title = "Sources & records"
                elif route == "source":
                    data = presenter.source(rid)
                    for asset in data["assets"]:
                        if "portrait_attribution" in asset:
                            asset["can_open"] = bool(portrait_root and asset["bytes_available"])
                            if asset["can_open"]:
                                asset["url"] = f"/portraits/{audience}/{asset['id']}"
                        else:
                            asset["can_open"] = bool(asset_url and asset["bytes_available"])
                    title = data["title"]
                elif route == "portrait":
                    # Scope is a restriction on the URL. Session and the current
                    # serving view independently decide authority for every fetch.
                    if audience != request.path_params["audience"] or portrait_root is None:
                        raise NotFound
                    asset = repository.get(rid, ["assets"])
                    if (
                        not asset
                        or not asset.get("portrait_attribution")
                        or not asset.get("bytes_available")
                    ):
                        raise NotFound
                    return portrait_response(request, asset, portrait_root)
                elif route == "asset":
                    asset = repository.get(rid, ["assets"])
                    if (
                        not asset
                        or asset.get("portrait_attribution")
                        or not asset["bytes_available"]
                        or not asset_url
                    ):
                        raise NotFound
                    url = asset_url(asset, audience)
                    if not url:
                        raise NotFound
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme != "https"
                        or not parsed.hostname
                        or parsed.username
                        or parsed.password
                    ):
                        raise RuntimeError("Invalid asset URL")
                    return RedirectResponse(url, status_code=303)
                else:
                    raise NotFound
                if route in {"home", "people", "sources"}:
                    data.update(token=repository.listing_token(), audience=audience, offset=offset)
                pagination = {}
                for key in ("next", "previous"):
                    if data.get(key) is not None:
                        params = {"offset": data[key]}
                        if route in {"home", "people", "sources"} and q:
                            params["q"] = q
                        if route == "sources" and kind:
                            params["kind"] = kind
                        if route in {"home", "people"}:
                            params["scope"] = scope
                        pagination[key] = "?" + urlencode(params)
                if route == "sources":
                    for key in ("next", "previous"):
                        data[key + "_url"] = (
                            "/api/sources" + pagination[key] if key in pagination else None
                        )
                if api:
                    return JSONResponse(data)
                return render_page(
                    "page.html",
                    title=title,
                    view=route,
                    data=data,
                    family_access=audience == "family",
                    access_state=access_state(request, audience == "family"),
                    public_fallback=getattr(request.state, "public_fallback", False),
                    public_document=True,
                    pagination=pagination,
                    feedback_url="/feedback?" + urlencode({"from": request.url.path})
                    if local_return(request.url.path)
                    else "/feedback",
                    access_url="/access?" + urlencode({"next": local_return(request.url.path)})
                    if rid and local_return(request.url.path)
                    else "/access",
                )
        except NotFound as exc:
            return not_found(request, exc)
        except Exception as exc:
            # No record IDs, queries, credentials or private DB errors in responses.
            return unavailable(request, exc)

    def me(request: Request) -> Response:
        try:
            if family_auth is None:
                return RedirectResponse("/access", status_code=303)
            identity = family_auth.identity(request)
            if identity is None:
                return RedirectResponse("/access?next=/me", status_code=303)
            if not family_auth.authorize(request):
                return RedirectResponse("/access", status_code=303)
            person = family_auth.store.current_person(identity)
            if person:
                # This is the separately authorized family reader, never authz's DSN.
                with store.open("family") as repository:
                    if repository.get(person, ["people"]):
                        return RedirectResponse(record_url("people", person), status_code=303)
            return render_page(
                "my_profile.html",
                title="My profile",
                family_access=True,
                access_state=family_auth.access_state(request),
            )
        except Exception as exc:
            return unavailable(request, exc)

    def stylesheet(request: Request) -> Response:
        return Response(
            ROOT.joinpath("static/site.css").read_text(),
            media_type="text/css",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    def static_asset(request: Request) -> Response:
        name = request.path_params.get("name", "favicon-32.png")
        types = {
            "inter-latin-normal.woff2": "font/woff2",
            "source-serif-4-latin-normal.woff2": "font/woff2",
            "source-serif-4-latin-italic.woff2": "font/woff2",
        }
        if name not in types:
            raise HTTPException(status_code=404)
        return Response(
            ROOT.joinpath("static", name).read_bytes(),
            media_type=types[name],
            headers={"Cache-Control": "public, max-age=3600"},
        )

    def script(request: Request) -> Response:
        name = request.path_params["name"] + ".js"
        if name not in {
            "access.js",
            "family-session.js",
            "listing.js",
            "navigation.js",
            "feedback.js",
        }:
            raise HTTPException(status_code=404)
        return Response(
            ROOT.joinpath("static", name).read_text(),
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    def access(request: Request) -> Response:
        return render_page(
            "message.html",
            title="Family access",
            message="Family sign-in is not available yet. You can continue exploring the public archive.",
        )

    def about(request: Request) -> Response:
        try:
            approved = document_audience(request) == "family"
            return render_page(
                "about.html",
                title="About this archive",
                family_access=approved,
                access_state=access_state(request, approved),
                public_fallback=getattr(request.state, "public_fallback", False),
                public_document=True,
            )
        except Exception as exc:
            return unavailable(request, exc)

    def robots(request: Request) -> Response:
        return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")

    def sitemap(request: Request) -> Response:
        # No hostname from untrusted Host headers and no private route inventory.
        return Response("Not found", status_code=404)

    routes = [
        Route("/", page),
        Route("/about", about),
        Route("/me", me),
        Route("/static/site.css", stylesheet),
        Route("/static/{name}.js", script),
        Route("/static/{name}", static_asset),
        Route("/robots.txt", robots),
        Route("/sitemap.xml", sitemap),
    ]
    # The packaged app, not just a test callback, owns the complete login flow.
    routes.extend(family_auth.routes(render_page) if family_auth else [Route("/access", access)])
    if family_auth and feedback is not None:
        routes.extend(feedback_routes(family_auth, feedback, render_page))
    for prefix in ("", "/api"):
        routes.extend(
            [
                Route(prefix + "/people", _view(page, "people")),
                Route(prefix + "/sources", _view(page, "sources")),
            ]
        )
        for path, view in (
            ("people", "person"),
            ("records", "persona"),
            ("family", "family"),
            ("assertions", "assertion"),
            ("sources", "source"),
        ):
            routes.append(Route(prefix + "/" + path + "/{record_id}", _view(page, view)))
    routes.append(Route("/portraits/{audience}/{record_id}", _view(page, "portrait")))
    routes.append(Route("/portraits/{audience}/{record_id}/thumbnail", _view(page, "portrait")))
    routes.append(Route("/assets/{record_id}", _view(page, "asset")))
    app = Starlette(
        routes=routes,
        exception_handlers={
            404: not_found,
            500: unavailable,
            AuthUnavailable: unavailable,
            AuthzConfigurationError: unavailable,
        },
    )

    async def response_policy(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": response.headers.get(
                    "Cache-Control", "private, no-store, max-age=0"
                ),
                "Content-Security-Policy": "default-src 'none'; img-src 'self'; font-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Robots-Tag": "noindex, noarchive",
            }
        )
        if family_auth and request.url.path in {"/access", "/access/complete"}:
            response.headers["Content-Security-Policy"] = response.headers[
                "Content-Security-Policy"
            ].replace(
                "connect-src 'self'",
                "connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com",
            )
            if (
                request.url.path == "/access"
                and getattr(request.state, "family_access_state", None) == "anonymous"
            ):
                # Google popup uses the SDK's GAPI loader and the configured
                # Firebase helper iframe only on the anonymous sign-in page.
                response.headers["Content-Security-Policy"] = response.headers[
                    "Content-Security-Policy"
                ].replace("script-src 'self'", "script-src 'self' https://apis.google.com") + (
                    "; frame-src https://" + family_auth.config.auth_domain
                )
        if (
            request.scope.get("endpoint") not in {stylesheet, static_asset, script}
            or response.status_code != 200
        ):
            response.headers["Vary"] = "Cookie, Authorization"
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=response_policy)
    return app


def portrait_response(request: Request, asset: Record, root: Path) -> Response:
    # The holder of a URL never receives a reusable storage credential. Files
    # are already stripped derivatives, named by their verified content digest.
    import re

    checksum = asset.get("sha256", "")
    if not re.fullmatch("[0-9a-f]{64}", checksum):
        raise NotFound
    path = root / checksum
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise NotFound
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if hashlib.sha256(data).hexdigest() != checksum or not safe_png(data):
        raise NotFound
    headers = {"Accept-Ranges": "bytes"}
    requested = request.headers.get("range")
    status = 200
    if requested:
        match = re.fullmatch(r"bytes=(\d{0,12})-(\d{0,12})", requested)
        size = len(data)
        if not match or not any(match.groups()):
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        first, last = match.groups()
        start = int(first) if first else max(0, size - int(last))
        end = min(int(last), size - 1) if first and last else size - 1
        if start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        data = data[start : end + 1]
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        status = 206
    headers["Content-Length"] = str(len(data))
    return Response(
        b"" if request.method == "HEAD" else data,
        media_type="image/png",
        status_code=status,
        headers=headers,
    )


def _view(endpoint: Callable[[Request], Response], view: str) -> Callable[[Request], Response]:
    def wrapped(request: Request) -> Response:
        request.path_params["view"] = view
        return endpoint(request)

    return wrapped


def profile_url(card: Record) -> str:
    return record_url("records" if card["kind"] == "Record subject" else "people", card["id"])


TEMPLATES.globals["profile_url"] = profile_url
TEMPLATES.globals["record_url"] = record_url
TEMPLATES.globals["source_kind_labels"] = SOURCE_KIND_LABELS


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run(
        "genealogy.web.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        access_log=False,
        proxy_headers=False,
    )
