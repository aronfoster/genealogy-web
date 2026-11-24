from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class FeedbackConfig:
    token: str = field(repr=False)
    base_url: str
    project_id: str
    reviewed_project_id: str

    def __post_init__(self) -> None:
        # JetBrains documents "perm:", but an authenticated YouTrack instance
        # can issue opaque permanent tokens beginning with "perm-". Do not
        # rewrite either token. Reject whitespace/control/header injection and
        # rely on the project preflight to verify provider authentication.
        if (
            not isinstance(self.token, str)
            or not self.token.startswith(("perm:", "perm-"))
            or not 6 <= len(self.token) <= 4096
            or not re.fullmatch(r"[!-~]+", self.token)
        ):
            raise ValueError("GENEALOGY_YOUTRACK_TOKEN must be a permanent token")
        try:
            url = urlsplit(self.base_url)
            valid = (
                url.scheme == "https"
                and url.netloc == url.hostname
                and bool(re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", url.hostname or ""))
                and not url.path
                and not url.query
                and not url.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("GENEALOGY_YOUTRACK_BASE_URL must be an HTTPS origin")
        if not re.fullmatch(r"[0-9]+-[0-9]+", self.project_id):
            raise ValueError("GENEALOGY_YOUTRACK_PROJECT_ID must be an internal project ID")
        if self.reviewed_project_id != self.project_id:
            raise ValueError("GENEALOGY_YOUTRACK_REVIEWED_PROJECT_ID must attest access review")

    @property
    def destination(self) -> str:
        # A changed destination must not turn an old browser UUID into a new POST.
        return hashlib.sha256(f"{self.base_url}\n{self.project_id}".encode()).hexdigest()

    @classmethod
    def from_env(cls) -> FeedbackConfig | None:
        mode = os.environ.get("GENEALOGY_FEEDBACK_MODE", "off")
        if mode == "off":
            return None
        if mode != "on":
            raise ValueError("GENEALOGY_FEEDBACK_MODE must be on or off")
        # Inspect names only: never read or fall back to retired credential values.
        if any(name.startswith("GENEALOGY_LINEAR_") for name in os.environ):
            raise ValueError("Remove retired GENEALOGY_LINEAR_* settings before enabling feedback")
        return cls(
            token=os.environ.get("GENEALOGY_YOUTRACK_TOKEN", ""),
            base_url=os.environ.get("GENEALOGY_YOUTRACK_BASE_URL", ""),
            project_id=os.environ.get("GENEALOGY_YOUTRACK_PROJECT_ID", ""),
            reviewed_project_id=os.environ.get("GENEALOGY_YOUTRACK_REVIEWED_PROJECT_ID", ""),
        )
