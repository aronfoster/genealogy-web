"""YouTrack REST only. No implicit HTTP retries and no client-assigned issue ID."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any, BinaryIO, ContextManager, Protocol, cast
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import FeedbackConfig
from .errors import DeliveryError, Issue, Rejected

TIMEOUT = 10
BUDGET = 20
MAX_RESPONSE_BYTES = 65_536
FIELDS = "id,idReadable,project(id,shortName),summary,description,isDraft"


class Opener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> ContextManager[BinaryIO]: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _open(request: Request, *, timeout: float) -> ContextManager[BinaryIO]:
    return cast(
        ContextManager[BinaryIO], build_opener(_NoRedirect()).open(request, timeout=timeout)
    )


def markers(key: str, owner: str, payload: str) -> str:
    return f"Reporter check: {owner}\nSubmission check: {key}\nPayload check: {payload}\n"


class YouTrackClient:
    def __init__(
        self,
        config: FeedbackConfig,
        *,
        opener: Opener | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._opener = opener or _open
        self.clock = clock

    def _request(
        self,
        path: str,
        *,
        deadline: float,
        data: dict[str, object] | None = None,
        query: dict[str, str | int] | None = None,
        project: bool = False,
        late_ok: bool = False,
    ) -> Any:
        timeout = min(TIMEOUT, deadline - self.clock())
        if timeout < 1:
            if data is not None:
                # Nothing reached the opener. Even after a slow ledger commit,
                # this is a definite refusal rather than an ambiguous create.
                raise Rejected("rejected")
            raise DeliveryError()
        args = {"fields": "id,shortName" if project else FIELDS, **(query or {})}
        resource = "/api/admin/projects" if project else "/api/issues"
        request = Request(
            self.config.base_url + resource + path + "?" + urlencode(args),
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": "Bearer " + self.config.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST" if data is not None else "GET",
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in {400, 401, 403, 429}:
                raise Rejected(
                    {401: "auth", 403: "auth", 429: "rate_limited"}.get(status, "rejected")
                ) from None
            raise DeliveryError() from None
        except Exception:
            raise DeliveryError() from None
        # A completed create may already be committed even if blocking I/O ran
        # past the cooperative budget. Preserve its receipt for full verification;
        # preflight/search reads still have to finish within their deadline.
        if len(raw) > MAX_RESPONSE_BYTES or (not late_ok and self.clock() >= deadline):
            raise DeliveryError()
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise DeliveryError() from None

    def verified(self, value: Any, key: str, owner: str, payload: str) -> Issue:
        if not isinstance(value, dict):
            raise DeliveryError()
        project = value.get("project")
        if (
            not isinstance(project, dict)
            or project.get("id") != self.config.project_id
            or project.get("shortName") != "GEN"
            or value.get("isDraft") is not False
            or not isinstance(value.get("id"), str)
            or not re.fullmatch(r"[0-9]+-[0-9]+", value["id"])
            or not isinstance(value.get("idReadable"), str)
            or not re.fullmatch(r"GEN-[1-9][0-9]*", value["idReadable"])
            or not isinstance(value.get("description"), str)
        ):
            raise DeliveryError()
        # Match trusted leading metadata only, never visitor text or a search hit.
        pattern = r"\s+".join(re.escape(line) for line in markers(key, owner, payload).splitlines())
        if not re.match(pattern + r"\b", value["description"].lstrip()):
            raise DeliveryError()
        return Issue(value["id"], value["idReadable"])

    def verify_project(self, *, deadline: float) -> None:
        # The API path does not require an administrator token. Check the target
        # before transmitting any private content, in addition to owner ACL review.
        try:
            value = self._request("/" + self.config.project_id, deadline=deadline, project=True)
            if (
                not isinstance(value, dict)
                or value.get("id") != self.config.project_id
                or value.get("shortName") != "GEN"
            ):
                raise DeliveryError()
        except Exception:
            # No create was attempted; a later request may safely retry preflight.
            raise Rejected("rejected") from None

    def create(
        self, title: str, body: str, key: str, owner: str, payload: str, *, deadline: float
    ) -> Issue:
        value = self._request(
            "",
            deadline=deadline,
            late_ok=True,
            data={
                "project": {"id": self.config.project_id},
                "summary": title,
                "description": body,
            },
        )
        return self.verified(value, key, owner, payload)

    def find(self, key: str, owner: str, payload: str, *, deadline: float) -> Issue | None:
        values = self._request(
            "",
            deadline=deadline,
            query={
                "query": f'project: GEN description: "{key}"',
                "$top": 50,
            },
        )
        if not isinstance(values, list) or len(values) >= 50:
            raise DeliveryError()
        found = []
        for value in values:
            try:
                found.append(self.verified(value, key, owner, payload))
            except DeliveryError:
                continue
        # Even two matching candidates are ambiguous; a search is not uniqueness.
        return found[0] if len(found) == 1 else None

    def read(self, issue: str, key: str, owner: str, payload: str, *, deadline: float) -> Issue:
        if not re.fullmatch(r"GEN-[1-9][0-9]*", issue):
            raise DeliveryError()
        value = self._request("/" + quote(issue, safe=""), deadline=deadline)
        return self.verified(value, key, owner, payload)
