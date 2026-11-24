"""Durable intents with a session lock spanning short committed transactions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from genealogy.auth.database import AuthzStore

from .errors import Conflict, DeliveryError, Issue


@dataclass(frozen=True)
class Receipt:
    key: str
    owner: str = field(repr=False)
    payload: str = field(repr=False)
    destination: str = field(repr=False)
    state: str
    issue_id: str | None
    identifier: str | None
    created_at: datetime


class Session:
    def __init__(self, connection: Any, key: str) -> None:
        self.connection = connection
        self.key = key

    def get(self) -> Receipt | None:
        row = self.connection.execute(
            "SELECT * FROM authz.feedback_get(%s)", (self.key,)
        ).fetchone()
        return self._receipt(row) if row is not None else None

    @staticmethod
    def _receipt(row: Any) -> Receipt:
        return Receipt(str(row[0]), *row[1:8])

    def claim(self, owner: str, payload: str, destination: str) -> Receipt:
        row = self.connection.execute(
            "SELECT * FROM authz.feedback_claim(%s,%s,%s,%s)",
            (self.key, owner, payload, destination),
        ).fetchone()
        # Commit acknowledged before any network side effect. A failed/lost
        # commit acknowledgement MUST escape; it never grants permission to POST.
        self.connection.commit()
        receipt = self._receipt(row)
        if (receipt.owner, receipt.payload, receipt.destination) != (owner, payload, destination):
            raise Conflict()
        return receipt

    def record(self, receipt: Receipt, state: str, issue: Issue | None = None) -> None:
        row = self.connection.execute(
            "SELECT authz.feedback_record(%s,%s,%s,%s,%s,%s,%s)",
            (
                self.key,
                receipt.owner,
                receipt.payload,
                receipt.destination,
                state,
                issue.id if issue else None,
                issue.identifier if issue else None,
            ),
        ).fetchone()
        self.connection.commit()
        if not row or row[0] is not True:
            raise DeliveryError()


class Ledger:
    def __init__(self, dsn: str) -> None:
        self._store = AuthzStore(dsn)

    @contextmanager
    def locked(self, key: str) -> Iterator[Session]:
        parsed = UUID(key)
        if str(parsed) != key or parsed.version != 4:
            raise Conflict()
        with self._store.connection() as connection:
            # AuthzStore first checks least privilege. Session settings survive
            # our intent commits; the connection closes (and unlocks) on all exits.
            connection.execute("SET ROLE genealogy_authz_runtime")
            connection.execute("SET statement_timeout='5s'")
            locked = connection.execute(
                "SELECT pg_try_advisory_lock(205,%s)",
                (int.from_bytes(parsed.bytes[:4], "big", signed=True),),
            ).fetchone()[0]
            connection.commit()
            if not locked:
                raise DeliveryError()
            yield Session(connection, key)
