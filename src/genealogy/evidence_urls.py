"""URL rules shared by corpus validation and reader presentation."""

from urllib.parse import urlsplit


def safe_evidence_url(url: str) -> bool:
    if any(c.isspace() or ord(c) < 32 or c in "\\<>\"'" for c in url):
        return False
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and (parsed.port is None or 0 < parsed.port <= 65535)
        )
    except ValueError:
        return False
