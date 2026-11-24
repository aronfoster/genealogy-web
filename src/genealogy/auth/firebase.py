from __future__ import annotations

import datetime
import time
import uuid
from typing import Any

import firebase_admin
from firebase_admin import auth

from .identity import Identity, InvalidIdentity

SESSION_SECONDS = 5 * 24 * 60 * 60
# Reader admission policy; the separate owner verifier retains its own policy.
READER_PROVIDERS = frozenset({"password", "google.com"})


class FirebaseVerifier:
    """Only credential failures become anonymous; transport/IAM errors propagate."""

    def __init__(self, project: str) -> None:
        self.project = project
        self.app = firebase_admin.initialize_app(
            options={"projectId": project, "httpTimeout": 5}, name="genealogy-" + uuid.uuid4().hex
        )

    def verify(self, token: str, *, session: bool) -> dict[str, Any]:
        try:
            if session:
                result = auth.verify_session_cookie(token, check_revoked=True, app=self.app)
            else:
                result = auth.verify_id_token(token, check_revoked=True, app=self.app)
            return dict(result)
        except (
            auth.InvalidIdTokenError,
            auth.InvalidSessionCookieError,
            auth.RevokedIdTokenError,
            auth.RevokedSessionCookieError,
            auth.UserDisabledError,
            auth.UserNotFoundError,
        ) as exc:
            raise InvalidIdentity("Invalid credentials") from exc

    def session_identity(self, cookie: str) -> Identity:
        return self._reader_identity(self.verify(cookie, session=True))

    def _reader_identity(self, claims: dict[str, Any]) -> Identity:
        firebase = claims.get("firebase")
        if (
            not isinstance(firebase, dict)
            or not isinstance(firebase.get("sign_in_provider"), str)
            or firebase["sign_in_provider"] not in READER_PROVIDERS
        ):
            raise InvalidIdentity("Invalid verified identity")
        return Identity.from_claims(claims, self.project)

    def exchange(self, token: str) -> tuple[Identity, str]:
        claims = self.verify(token, session=False)
        identity = self._reader_identity(claims)
        auth_time = claims.get("auth_time")
        if not isinstance(auth_time, (int, float)) or not 0 <= time.time() - auth_time <= 300:
            raise InvalidIdentity("Sign in again to continue")
        cookie = auth.create_session_cookie(
            token, expires_in=datetime.timedelta(seconds=SESSION_SECONDS), app=self.app
        )
        return identity, str(cookie)

    def close(self) -> None:
        firebase_admin.delete_app(self.app)
