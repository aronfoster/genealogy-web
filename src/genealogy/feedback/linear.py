"""Retired Linear provider, retained for historical regression evidence only."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.client import HTTPException
from typing import Any, BinaryIO, ContextManager, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from .formatting import MESSAGE_HEADING, OWNER_PREFIX
from .legacy import FeedbackConfig

ENDPOINT = "https://api.linear.app/graphql"
TIMEOUT = 10
BUDGET = 20
MAX_RESPONSE_BYTES = 65_536
FIND = "query($id:String!){issue(id:$id){id identifier url description}}"
CREATE = (
    "mutation($input:IssueCreateInput!){"
    "issueCreate(input:$input){success issue{id identifier url}}}"
)
Category = Literal["network", "auth", "rate_limited", "rejected", "unexpected"]


class DeliveryError(Exception):
    """Only a fixed category may cross the delivery boundary."""

    def __init__(self, category: Category) -> None:
        self.category: Category = (
            category
            if category in {"network", "auth", "rate_limited", "rejected", "unexpected"}
            else "unexpected"
        )
        super().__init__(self.category)


class Conflict(Exception):
    def __init__(self) -> None:
        super().__init__("Feedback reference conflict")


@dataclass(frozen=True)
class Issue:
    id: str
    identifier: str
    url: str
    description: str = field(default="", repr=False)


class Opener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> ContextManager[BinaryIO]: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        # urllib otherwise forwards custom headers, including Authorization.
        return None


def _open(request: Request, *, timeout: float) -> ContextManager[BinaryIO]:
    return cast(
        ContextManager[BinaryIO], build_opener(_NoRedirect()).open(request, timeout=timeout)
    )


class LinearClient:
    def __init__(
        self,
        config: FeedbackConfig,
        *,
        opener: Opener | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._opener: Opener = opener if opener is not None else _open
        self._clock = clock

    def _request(
        self,
        query: str,
        variables: dict[str, object],
        *,
        finding: bool = False,
        deadline: float | None = None,
    ) -> dict[str, Any] | None:
        timeout = TIMEOUT if deadline is None else min(TIMEOUT, deadline - self._clock())
        if timeout < 1:
            raise DeliveryError("network")
        status = 200
        try:
            request = Request(
                ENDPOINT,
                data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
                headers={"Authorization": self.config.api_key, "Content-Type": "application/json"},
                method="POST",
            )
            with self._opener(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1) if status == 400 else b""
            except Exception:
                raw = b""
            finally:
                exc.close()
        except (OSError, URLError, HTTPException):
            raise DeliveryError("network") from None
        except Exception:
            raise DeliveryError("unexpected") from None

        if deadline is not None and self._clock() >= deadline:
            raise DeliveryError("network")
        if status in {401, 403}:
            raise DeliveryError("auth")
        if status == 429:
            raise DeliveryError("rate_limited")
        if status >= 500:
            raise DeliveryError("network")
        if status not in {200, 400}:
            raise DeliveryError("rejected")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DeliveryError("unexpected")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise DeliveryError("unexpected") from None
        if not isinstance(payload, dict):
            raise DeliveryError("unexpected")
        errors = payload.get("errors")
        if errors is not None and errors != []:
            if not isinstance(errors, list) or not all(isinstance(e, dict) for e in errors):
                raise DeliveryError("unexpected")
            codes = set()
            for error in errors:
                extensions = error.get("extensions")
                if isinstance(extensions, dict):
                    for key in ("code", "type"):
                        value = extensions.get(key)
                        if isinstance(value, str):
                            codes.add(value.upper().replace(" ", "_"))
            if codes & {"UNAUTHENTICATED", "AUTHENTICATION_ERROR", "FORBIDDEN"}:
                raise DeliveryError("auth")
            if codes & {"RATELIMITED", "RATE_LIMITED"}:
                raise DeliveryError("rate_limited")
            if finding and all(self._missing(error) for error in errors):
                return None
            raise DeliveryError("rejected")
        if status != 200:
            raise DeliveryError("rejected")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise DeliveryError("unexpected")
        return data

    @staticmethod
    def _missing(error: dict[str, Any]) -> bool:
        if error.get("path") not in (None, ["issue"]):
            return False
        extensions = error.get("extensions", {})
        if not isinstance(extensions, dict):
            return False
        code = extensions.get("code")
        return (isinstance(code, str) and code in {"NOT_FOUND", "ENTITY_NOT_FOUND"}) or error.get(
            "message"
        ) == "Entity not found: Issue"

    @staticmethod
    def _issue(value: object, issue_id: str) -> Issue:
        if not isinstance(value, dict) or any(
            not isinstance(value.get(key), str) or not value[key]
            for key in ("id", "identifier", "url")
        ):
            raise DeliveryError("unexpected")
        if value["id"] != issue_id:
            raise DeliveryError("unexpected")
        text = value.get("description")
        if text is not None and not isinstance(text, str):
            raise DeliveryError("unexpected")
        return Issue(value["id"], value["identifier"], value["url"], text or "")

    def find(self, issue_id: str | UUID, *, deadline: float | None = None) -> Issue | None:
        key = str(issue_id)
        data = self._request(FIND, {"id": key}, finding=True, deadline=deadline)
        if data is None:
            return None
        if "issue" not in data:
            raise DeliveryError("unexpected")
        if data["issue"] is None:
            return None
        return self._issue(data["issue"], key)

    def create(
        self, issue_id: str | UUID, title: str, body: str, *, deadline: float | None = None
    ) -> Issue:
        key = str(issue_id)
        data = self._request(
            CREATE,
            {
                "input": {
                    "id": key,
                    "teamId": self.config.team_id,
                    "stateId": self.config.state_id,
                    "title": title,
                    "description": body,
                }
            },
            deadline=deadline,
        )
        result = data.get("issueCreate") if data is not None else None
        if not isinstance(result, dict) or result.get("success") is not True:
            raise DeliveryError("rejected")
        return self._issue(result.get("issue"), key)

    @staticmethod
    def _owned(issue: Issue, owner_token: str) -> Issue:
        # Linear can reserialize email links and join metadata lines. The token
        # leads the header, so a marker inside a URL or message cannot claim it.
        metadata = issue.description.partition(MESSAGE_HEADING)[0]
        metadata = re.split(r"(?m)^ {0,3}(?:`{3,}|~{3,})", metadata, maxsplit=1)[0]
        if not re.fullmatch(r"[0-9a-f]{32}", owner_token) or not re.match(
            re.escape(OWNER_PREFIX) + owner_token + r"\b", metadata.lstrip()
        ):
            raise Conflict()
        return issue

    def ensure(self, issue_id: str | UUID, title: str, body: str, owner_token: str) -> Issue:
        """Share a 20s budget across calls; urllib timeouts are socket, not wall-clock limits."""
        deadline = self._clock() + BUDGET
        existing = self.find(issue_id, deadline=deadline)
        if existing is not None:
            return self._owned(existing, owner_token)
        try:
            return self.create(issue_id, title, body, deadline=deadline)
        except DeliveryError as error:
            # Resolve ambiguous creation exactly once; never repeat the mutation.
            if deadline - self._clock() < 1:
                raise DeliveryError("network") from None
            try:
                existing = self.find(issue_id, deadline=deadline)
            except DeliveryError:
                raise DeliveryError(error.category) from None
            if existing is not None:
                return self._owned(existing, owner_token)
            raise DeliveryError(error.category) from None
