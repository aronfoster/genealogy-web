"""At most one ambiguous create per UUID, rather than an exactly-once claim."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from uuid import UUID

from .errors import Conflict, DeliveryError, Issue, Rejected
from .formatting import MESSAGE_HEADING, OWNER_PREFIX
from .youtrack import BUDGET, YouTrackClient, markers

RECOVERY_SECONDS = 3


def fingerprint(title: str, body: str) -> str:
    metadata, separator, message = body.partition(MESSAGE_HEADING)
    # Server-generated time changes between requests. Nothing inside visitor
    # prose is removed, even if it imitates a timestamp/metadata line.
    metadata = re.sub(r"(?m)^Submitted \(UTC\): [^\n]+\n", "", metadata)
    return hashlib.sha256((title + "\n" + metadata + separator + message).encode()).hexdigest()


class FeedbackDelivery:
    def __init__(self, client: YouTrackClient, ledger: Any) -> None:
        self.client = client
        self.ledger = ledger

    def ensure(self, key: str, title: str, body: str, owner: str) -> Issue:
        parsed = UUID(key)
        if str(parsed) != key or parsed.version != 4 or not re.fullmatch(r"[0-9a-f]{32}", owner):
            raise Conflict()
        if not body.startswith(OWNER_PREFIX + owner + "\n"):
            raise Conflict()
        payload = fingerprint(title, body)
        deadline = self.client.clock() + BUDGET
        with self.ledger.locked(key) as session:
            receipt = session.claim(owner, payload, self.client.config.destination)
            if receipt.state == "received":
                return Issue(receipt.issue_id, receipt.identifier)
            if receipt.state == "pending":
                return self._recover(session, receipt, deadline)
            self.client.verify_project(deadline=deadline)
            if deadline - RECOVERY_SECONDS - self.client.clock() < 1:
                # No POST was attempted. Keep the bound report retryable rather
                # than committing a pending intent that recovery can never find.
                raise Rejected("rejected")
            # The pending barrier is committed BEFORE calling the transport.
            # A crash from this point (even before POST) leaves a read-only retry.
            session.record(receipt, "pending")
            submitted_body = markers(key, owner, payload) + body.split("\n", 1)[1]
            try:
                issue = self.client.create(
                    # Reserve time for a read-only lookup after a timed-out POST.
                    # A late valid create receipt is still verified and accepted.
                    title,
                    submitted_body,
                    key,
                    owner,
                    payload,
                    deadline=deadline - RECOVERY_SECONDS,
                )
            except Rejected:
                session.record(receipt, "rejected")
                raise
            except Exception:
                return self._recover(session, receipt, deadline)
            session.record(receipt, "received", issue)
            return issue

    def _recover(self, session: Any, receipt: Any, deadline: float) -> Issue:
        try:
            issue = self.client.find(receipt.key, receipt.owner, receipt.payload, deadline=deadline)
        except Exception:
            raise DeliveryError() from None
        if issue is None:
            raise DeliveryError()
        session.record(receipt, "received", issue)
        return issue

    def reconcile(self, key: str, issue_id: str | None = None) -> Issue:
        """Owner route: read and confirm only. Never creates or resets an intent."""
        deadline = self.client.clock() + BUDGET
        with self.ledger.locked(key) as session:
            receipt = session.get()
            if receipt is None or receipt.destination != self.client.config.destination:
                raise Conflict()
            if receipt.state == "received":
                return Issue(receipt.issue_id, receipt.identifier)
            if receipt.state != "pending":
                raise Conflict()
            if issue_id is None:
                return self._recover(session, receipt, deadline)
            issue = self.client.read(
                issue_id, receipt.key, receipt.owner, receipt.payload, deadline=deadline
            )
            session.record(receipt, "received", issue)
            return issue
