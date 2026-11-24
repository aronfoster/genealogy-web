from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AuthConfig:
    origin: str
    project: str
    api_key: str
    app_id: str
    auth_domain: str
    csrf_secret: str
    dsn: str
    proxy_cidrs: tuple[str, ...] = ()
    proxy_header: str = ""
    exchange_limit: int = 20
    request_limit: int = 10
    rate_window: int = 600

    def __post_init__(self) -> None:
        url = urlsplit(self.origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError("GENEALOGY_ORIGIN must be an HTTPS origin without a trailing slash")
        if not all((self.project, self.api_key, self.app_id, self.auth_domain, self.dsn)):
            raise ValueError("Family authentication configuration is incomplete")
        # This hostname also becomes an exact CSP frame source. Reject URLs,
        # ports, whitespace and policy directives rather than widening the policy.
        if len(self.auth_domain) > 253 or not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in self.auth_domain.split(".")
        ):
            raise ValueError("Firebase authDomain must be a DNS hostname")
        if len(self.csrf_secret) < 32:
            raise ValueError("GENEALOGY_CSRF_SECRET must have at least 32 random characters")
        if self.proxy_header not in {"", "x-forwarded-for", "cf-connecting-ip"}:
            raise ValueError("Unsupported trusted proxy header")
        if bool(self.proxy_header) != bool(self.proxy_cidrs):
            raise ValueError("Trusted proxy header and CIDRs must be configured together")
        for network in self.proxy_cidrs:
            ipaddress.ip_network(network)
        if min(self.exchange_limit, self.request_limit, self.rate_window) < 1:
            raise ValueError("Rate limits must be positive")

    @property
    def web(self) -> dict[str, str]:
        return {
            "apiKey": self.api_key,
            "appId": self.app_id,
            "projectId": self.project,
            "authDomain": self.auth_domain,
        }

    @classmethod
    def from_env(cls) -> AuthConfig | None:
        # Explicit public-only mode remains the default. Unsupported switches are errors.
        if any(
            key in os.environ
            for key in (
                "FIREBASE_AUTH_EMULATOR_HOST",
                "FIREBASE_EMULATOR_HUB",
                "GENEALOGY_AUTH_BYPASS",
                "GENEALOGY_DEV_AUTH",
            )
        ):
            raise ValueError(
                "Emulator and bypass configuration is not supported by this executable"
            )
        mode = os.environ.get("GENEALOGY_AUTH_MODE", "public")
        if mode == "public":
            return None
        if mode != "family":
            raise ValueError("GENEALOGY_AUTH_MODE must be public or family")
        if not os.environ.get("GENEALOGY_FAMILY_DSN"):
            raise ValueError("Family reader DSN is required")
        web = json.loads(os.environ.get("GENEALOGY_FIREBASE_WEB_CONFIG", "{}"))
        return cls(
            origin=os.environ.get("GENEALOGY_ORIGIN", ""),
            project=web.get("projectId", ""),
            api_key=web.get("apiKey", ""),
            app_id=web.get("appId", ""),
            auth_domain=web.get("authDomain", ""),
            csrf_secret=os.environ.get("GENEALOGY_CSRF_SECRET", ""),
            dsn=os.environ.get("GENEALOGY_AUTHZ_DSN", ""),
            proxy_cidrs=tuple(
                filter(None, os.environ.get("GENEALOGY_TRUSTED_PROXY_CIDRS", "").split(","))
            ),
            proxy_header=os.environ.get("GENEALOGY_CLIENT_IP_HEADER", "").lower(),
            exchange_limit=int(os.environ.get("GENEALOGY_EXCHANGE_LIMIT", "20")),
            request_limit=int(os.environ.get("GENEALOGY_REQUEST_LIMIT", "10")),
            rate_window=int(os.environ.get("GENEALOGY_RATE_WINDOW", "600")),
        )
