from __future__ import annotations

from dataclasses import dataclass


class DeliveryError(Exception):
    """No private provider/DB exception text crosses this boundary."""

    def __init__(self, category: str = "pending") -> None:
        self.category = (
            category if category in {"pending", "auth", "rate_limited", "rejected"} else "pending"
        )
        super().__init__(self.category)


class Rejected(DeliveryError):
    """A definite API refusal; never used for 5xx, parsing, or I/O failures."""


class Conflict(Exception):
    def __init__(self) -> None:
        super().__init__("Feedback reference conflict")


@dataclass(frozen=True)
class Issue:
    id: str
    identifier: str
