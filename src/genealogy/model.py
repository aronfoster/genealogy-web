from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PREFIXES = {
    "person": "per",
    "persona": "psn",
    "assertion": "asn",
    "source": "src",
    "citation": "cit",
    "place": "plc",
    "asset": "ast",
    "narrative": "nar",
    "consent": "con",
    "research_question": "rq",
}
VISIBILITY_RANK = {"withheld": 0, "admin": 1, "family": 2, "public": 3}


def canonical_bytes(value: Any) -> bytes:
    """Return the only permitted on-disk serialization."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def read_records(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    records: list[tuple[Path, dict[str, Any]]] = []
    if not root.is_dir():
        raise RecordReadError(root, "corpus directory does not exist")
    for path in sorted(root.rglob("*.json")):
        try:
            value = json.loads(path.read_text(), object_pairs_hook=_unique_keys)
        except (ValueError, UnicodeError, OSError) as exc:
            raise RecordReadError(path, str(exc)) from exc
        if not isinstance(value, dict):
            raise RecordReadError(path, "record must be an object")
        records.append((path, value))
    return records


def write_record(root: Path, record: dict[str, Any]) -> Path:
    directory = root / str(record["type"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['id']}.json"
    path.write_bytes(canonical_bytes(record))
    return path


class RecordReadError(ValueError):
    def __init__(self, path: Path, detail: str):
        self.path = path
        super().__init__(detail)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
