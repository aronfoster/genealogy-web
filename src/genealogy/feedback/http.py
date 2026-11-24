"""Approved-family feedback intake with durable, fail-closed YouTrack delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from uuid import UUID

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from genealogy.auth.http import FamilyAuth, local_return

from .errors import Conflict, DeliveryError, Rejected
from .formatting import REQUEST_TYPES, issue_body, issue_title, owner_token, reference

LOGGER = logging.getLogger(__name__)


def routes(auth: FamilyAuth, client: Any, render: Callable[..., Response]) -> list[Route]:
    def form(request: Request) -> Response:
        if not auth.authorize(request):
            return RedirectResponse("/access?next=/feedback", status_code=303)
        return render(
            "feedback.html",
            title="Suggest a change",
            family_access=True,
            access_state="approved",
            page_path=local_return(request.query_params.get("from")),
        )

    def process(request: Request, data: dict[str, Any]) -> Response:
        ref = "unassigned"
        try:
            identity = auth.identity(request)
            if identity is None:
                return auth._error(401)
            if not auth.authorize(request):
                return auth._error(403)
            if not auth._limit(request, "feedback", identity):
                return auth._error(429)
            try:
                key = data.get("clientKey")
                if not isinstance(key, str):
                    return auth._error(400)
                parsed = UUID(key)
                if parsed.version != 4 or str(parsed) != key:
                    return auth._error(400)
                kind = data.get("kind", "other")
                message = data.get("message")
                if (
                    not isinstance(kind, str)
                    or kind not in REQUEST_TYPES
                    or not isinstance(message, str)
                    or any(unicodedata.category(c) == "Cc" and c != "\n" for c in message)
                ):
                    return auth._error(400)
                message = message.strip()
                if not 1 <= len(message) <= 4000:
                    return auth._error(400)
                path = local_return(data.get("pagePath"))
                origin = auth.config.origin
                person = auth.store.current_person(identity)
                ref = reference(parsed)
                title = issue_title(kind, ref)
                body = issue_body(
                    ref=ref,
                    kind=kind,
                    page_url=origin + quote(path, safe="/") if path else None,
                    person_url=origin + "/people/" + quote(person, safe="") if person else None,
                    reporter_email=identity.email,
                    submitted_at=datetime.now(timezone.utc),
                    message=message,
                    reporter_identity=hashlib.sha256(json.dumps(identity.key).encode()).hexdigest(),
                )
                token = owner_token(identity.email)
            except ValueError:
                # Formatter errors can include private metadata. Never log values.
                return auth._error(400)
            issue = client.ensure(str(parsed), title, body, token)
            LOGGER.info("feedback: result=received ref=%s", ref)
            return JSONResponse({"reference": ref, "issue": issue.identifier})
        except Conflict:
            return JSONResponse({"error": "Reference conflict"}, status_code=409)
        except DeliveryError as exc:
            LOGGER.warning("feedback: result=%s ref=%s", exc.category, ref)
            if isinstance(exc, Rejected):
                return JSONResponse({"error": "Not sent", "delivery": "rejected"}, status_code=503)
            return JSONResponse({"delivery": "pending", "reference": ref}, status_code=202)
        except Exception as exc:
            LOGGER.error("feedback: result=%s", type(exc).__name__)
        return JSONResponse(
            {"error": "Delivery unconfirmed", "delivery": "unknown"}, status_code=503
        )

    async def submit(request: Request) -> Response:
        if not auth._trusted(request, mutation=True):
            return auth._error(403)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return auth._error(400)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 20000:
                return auth._error(400)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            return auth._error(400)
        if not isinstance(data, dict):
            return auth._error(400)
        # Membership/profile I/O and the cooperative YouTrack budget must not block
        # the single uvicorn worker's event loop.
        return await run_in_threadpool(process, request, data)

    return [Route("/feedback", form), Route("/api/feedback", submit, methods=["POST"])]
