from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from email_validator import EmailNotValidError, validate_email


class InvalidIdentity(ValueError):
    """Invalid credentials: safe to treat as anonymous, unlike service outages."""


def normalize_email(value: str) -> tuple[str, str]:
    original = value.strip()
    if not original.isascii():
        raise InvalidIdentity("Use an ASCII email address, including a punycode domain if needed.")
    try:
        validate_email(original, check_deliverability=False, allow_smtputf8=False)
    except EmailNotValidError as exc:
        raise InvalidIdentity("Enter a valid email address.") from exc
    return original, original.lower()


@dataclass(frozen=True)
class Identity:
    project: str
    uid: str
    original_email: str
    email: str

    @classmethod
    def from_claims(cls, claims: dict[str, Any], project: str) -> Identity:
        uid, email = claims.get("sub"), claims.get("email")
        if (
            claims.get("aud") != project
            or not isinstance(uid, str)
            or not 1 <= len(uid) <= 128
            or not isinstance(email, str)
            or claims.get("email_verified") is not True
            or claims.get("firebase", {}).get("tenant")
        ):
            raise InvalidIdentity("Invalid verified identity")
        original, normalized = normalize_email(email)
        return cls(project, uid, original, normalized)

    @property
    def key(self) -> tuple[str, str, str]:
        return self.project, self.uid, self.email
