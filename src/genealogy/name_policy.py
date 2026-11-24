"""Exact, reviewed publication occurrences; never a global name allowlist."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .model import PREFIXES, _unique_keys

COLLECTION_PREFIXES = {
    "people": "per",
    "personas": "psn",
    "assertions": "asn",
    "history": "asn",
    "alternatives": "asn",
    "places": "plc",
    "narratives": "nar",
    "assets": "ast",
    "sources": "src",
    "citations": "cit",
}
Selector = tuple[str, str, str, tuple[str | int, ...]]


def fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


@dataclass(frozen=True, slots=True)
class NameOccurrence:
    audience: str
    collection: str
    record_id: str
    path: tuple[str | int, ...]
    value: str
    names: tuple[str, ...]

    @property
    def selector(self) -> Selector:
        return self.audience, self.collection, self.record_id, self.path


@dataclass(frozen=True, slots=True)
class NamePolicy:
    occurrences: tuple[NameOccurrence, ...] = ()
    source_bytes: bytes | None = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path) -> NamePolicy:
        raw = path.read_bytes()
        return replace(
            cls.from_document(json.loads(raw, object_pairs_hook=_unique_keys)), source_bytes=raw
        )

    @classmethod
    def from_document(cls, document: Any) -> NamePolicy:
        def invalid() -> ValueError:
            return ValueError(
                "name policy must contain unique, exact audience/record/field occurrences"
            )

        if (
            not isinstance(document, dict)
            or set(document) != {"version", "occurrences"}
            or type(document["version"]) is not int
            or document["version"] != 1
            or not isinstance(document["occurrences"], list)
        ):
            raise invalid()
        occurrences = []
        selectors = set()
        for row in document["occurrences"]:
            if not isinstance(row, dict) or set(row) != {
                "audience",
                "collection",
                "record_id",
                "path",
                "value",
                "names",
            }:
                raise invalid()
            if not all(
                isinstance(row[k], str) for k in ("audience", "collection", "record_id", "value")
            ):
                raise invalid()
            if (
                row["audience"] not in {"public", "family"}
                or row["collection"] not in COLLECTION_PREFIXES
            ):
                raise invalid()
            if not re.fullmatch(
                COLLECTION_PREFIXES[row["collection"]]
                + r"_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                row["record_id"],
            ):
                raise invalid()
            path, names = row["path"], row["names"]
            if (
                not isinstance(path, list)
                or not path
                or not isinstance(path[0], str)
                or any(
                    not ((isinstance(p, str) and p and p != "*") or (type(p) is int and p >= 0))
                    for p in path
                )
                or not isinstance(names, list)
                or not names
                or any(not isinstance(n, str) or not n.strip() or n != n.strip() for n in names)
            ):
                raise invalid()
            normalized = [fold(n) for n in names]
            if len(set(normalized)) != len(names) or any(
                n.startswith(tuple(p + "_" for p in PREFIXES.values()))
                or n not in fold(row["value"])
                for n in normalized
            ):
                raise invalid()
            occurrence = NameOccurrence(
                row["audience"],
                row["collection"],
                row["record_id"],
                tuple(path),
                row["value"],
                tuple(sorted(names)),
            )
            if occurrence.selector in selectors:
                raise invalid()
            selectors.add(occurrence.selector)
            occurrences.append(occurrence)
        return cls(tuple(occurrences))

    def document(self) -> dict[str, Any]:
        # Stable generation binding, independent of policy row/name ordering.
        rows = [
            dict(
                audience=o.audience,
                collection=o.collection,
                record_id=o.record_id,
                path=list(o.path),
                value=o.value,
                names=list(o.names),
            )
            for o in self.occurrences
        ]
        return {
            "version": 1,
            "occurrences": sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)),
        }

    def scan(self, document: dict[str, Any], forbidden: set[str]) -> set[Selector]:
        """Scan decoded strings/keys, intentionally excluding numeric and boolean values.

        Names occur in text. Exact string-leaf binding is required for an exception;
        hidden IDs still receive the separate, unconditional serialized-output scan.
        Return matched selectors so full-corpus callers can reject dormant rows.
        """
        allowed = {o.selector: o for o in self.occurrences}
        used: set[Selector] = set()

        def check(value: str, path: tuple[str | int, ...], *, key: bool = False) -> None:
            exempt: set[str] = set()
            if not key and len(path) >= 3 and isinstance(path[0], str) and type(path[1]) is int:
                collection = path[0]
                item = document[collection][path[1]]
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    selector = (document["audience"], collection, item["id"], path[2:])
                    occurrence = allowed.get(selector)
                    if occurrence is not None and value == occurrence.value:
                        if selector in used:
                            raise ValueError("publication text leak: duplicate reviewed occurrence")
                        used.add(selector)
                        exempt = {fold(n) for n in occurrence.names}
            leaked = sorted(
                n for n in forbidden if n and fold(n) in fold(value) and fold(n) not in exempt
            )
            if leaked:
                raise ValueError(f"publication text leak: {', '.join(leaked)}")

        def walk(value: Any, path: tuple[str | int, ...] = ()) -> None:
            if isinstance(value, str):
                check(value, path)
            elif isinstance(value, dict):
                for k, child in value.items():
                    check(k, path, key=True)
                    walk(child, (*path, k))
            elif isinstance(value, list):
                for i, child in enumerate(value):
                    walk(child, (*path, i))

        walk(document)
        return used

    def assert_all_used(self, *documents: dict[str, Any]) -> None:
        """Require every reviewed row to occur across the supplied audience documents."""
        used = set().union(*(self.scan(document, set()) for document in documents))
        unused = {o.selector for o in self.occurrences} - used
        if unused:
            raise ValueError(f"unused name policy occurrences: {len(unused)}")
