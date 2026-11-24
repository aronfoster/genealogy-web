"""Internal reader links with encoded path segments and fragments."""

from urllib.parse import quote


def record_url(section: str, record_id: str, fragment: str = "") -> str:
    url = "/" + section + "/" + quote(record_id, safe="")
    return url + "#" + quote(fragment, safe="") if fragment else url
