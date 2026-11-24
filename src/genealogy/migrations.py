from __future__ import annotations

from pathlib import Path
from typing import Any

from .model import read_records, write_record


def migrate_v0_to_v1(record: dict[str, Any]) -> dict[str, Any]:
    """Sample deterministic/idempotent migration from the pre-versioned draft."""
    migrated = dict(record)
    if migrated.get("schema_version", 0) == 0:
        migrated["schema_version"] = 1
        migrated.setdefault("retired", False)
        migrated.setdefault("visibility", "withheld")
    return migrated


def migrate_corpus(root: Path, from_version: int) -> None:
    if from_version != 0:
        raise ValueError("only migration 0 -> 1 exists")
    pairs = read_records(root)
    if any(r.get("schema_version", 0) not in (0, 1) for _, r in pairs):
        raise ValueError("migration refuses unknown schema versions")
    for path, record in pairs:
        migrated = migrate_v0_to_v1(record)
        if migrated["id"] != path.stem:
            raise ValueError(f"migration cannot repair mismatched identity: {path}")
        write_record(root, migrated)
