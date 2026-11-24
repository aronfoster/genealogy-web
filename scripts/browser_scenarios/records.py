"""Invented Glassharbor workshop catalogue. No checkout or source records are read."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from genealogy.model import PREFIXES, canonical_bytes, write_record


def identifier(kind: str, number: int, *, historical: bool = False) -> str:
    namespace = "01755555" if historical else "01777777"
    return f"{PREFIXES[kind]}_{namespace}-1111-7000-8000-{number:012x}"


def build(root: Path, *, historical: bool) -> None:
    corpus = root / "corpus"
    sequence = 1000

    def rid(kind: str, number: int) -> str:
        return identifier(kind, number, historical=historical)

    def row(kind: str, number: int, *, visibility: str = "public", **fields: Any) -> dict[str, Any]:
        record = dict(
            type=kind,
            id=rid(kind, number),
            visibility=visibility,
            schema_version=1,
            retired=False,
            **fields,
        )
        write_record(corpus, record)
        return record

    for n in range(1, 4 if historical else 83):
        kind = "research_report" if n <= 37 else "record_image" if n <= 70 else "newspaper"
        title = "Kilnglass" if n in (1, 2, 38, 39, 40) else "Harbor inventory"
        row(
            "source",
            n,
            source_kind=kind,
            title=f"{title} folio {n}",
            redistribution="restricted",
            rights_basis="unknown",
        )
    citation = row(
        "citation",
        1,
        source_id=rid("source", 1),
        locator="Tide kiln slip",
        inspection="original_image_inspected",
        transcription="Workshop reading",
        transcription_visibility="public",
    )
    evidence = [dict(citation_id=citation["id"], stance="supports", explicitness="explicit")]
    persona = row(
        "persona", 1, raw_name="Glaze workshop clerk", role="subject", citation_id=citation["id"]
    )
    statement = row(
        "assertion",
        1,
        assertion_type="person.name",
        subject_id=persona["id"],
        value={"raw": "Glaze workshop clerk"},
        dependencies=[],
        evidence=evidence,
        origin="source_statement",
        status="accepted",
    )

    def claim(
        n: int,
        typ: str,
        value: dict[str, Any],
        *,
        status: str = "accepted",
        visibility: str = "public",
        **fields: Any,
    ) -> dict[str, Any]:
        nonlocal sequence
        sequence += 1
        return row(
            "assertion",
            sequence,
            assertion_type=typ,
            subject_id=rid("person", n),
            visibility=visibility,
            value=value,
            status=status,
            origin="researcher_conclusion",
            reasoning="Invented workshop roll and tally agree on this entry.",
            dependencies=[statement["id"]],
            evidence=evidence,
            **fields,
        )

    names = {
        1: "Oriel Tidecup",
        2: "Senna Tidecup",
        3: "Vesper Tidecup",
        4: "Lumen Kilnwright",
        5: "Tern Lattice",
        6: "Nacre Reedwheel",
        90: "Pewter Wayfarer",
        91: "Opal Latch",
        92: "Umber Hinge",
        93: "Ochre Spindle",
    }
    if historical:
        names = {
            1: "Lark Flintsail",
            2: "Wren Flintsail",
            3: "Pip Flintsail",
            4: "Gull Seedlass",
            5: "Ibis Keelmark",
        }
    for n in range(1, 6) if historical else range(1, 95):
        row("person", n)
        claim(n, "person.name", {"raw": names.get(n, f"Workshop member {n:03}")})
        year = (1832 if n == 3 else 1840 + n) if historical else (1864 if n == 4 else 1840 + n % 61)
        claim(n, "person.death", {"date": {"raw": str(year), "normalized": f"{year}-09-08"}})
        if 2 <= n <= (3 if historical else 87):
            # A branching tree keeps profile groups small while exercising many listing pages.
            claim(
                n,
                "relationship.parent_child",
                {
                    "parent_id": rid("person", n // 2),
                    "child_id": rid("person", n),
                    "relationship_kind": "biological",
                },
                grade="A",
            )
    for disputed_year in ("1791", "1793"):
        claim(
            1,
            "person.birth",
            {"date": {"raw": disputed_year}},
            status="conflicting",
            conflict_group="cfg_workshop-birth-registers",
        )
    if historical:
        claim(3, "person.birth", {"date": {"raw": "1832"}})
        claim(1, "relationship.spouse", {"other_id": rid("person", 4), "raw": "Spouse"}, grade="A")
        claim(
            2,
            "relationship.parent_child",
            {
                "child_id": rid("person", 2),
                "parent_id": rid("person", 4),
                "relationship_kind": "unknown",
            },
            status="candidate",
            grade="D",
        )
        (root / "record-map.json").write_bytes(
            canonical_bytes(
                {key: rid("person", n) for n, key in enumerate(("senior", "child", "infant"), 1)}
            )
        )
        return

    # A historical window is deliberately unsuitable for a compact birth year.
    claim(
        4, "person.birth", {"date": {"raw": "during the first kiln season", "precision": "unknown"}}
    )
    event = claim(11, "event.adoption", {"date": {"raw": "1835"}})
    for child, parent, kind in ((11, 4, "adoptive"), (12, 4, "step"), (13, 4, "guardian")):
        link = {
            "child_id": rid("person", child),
            "parent_id": rid("person", parent),
            "relationship_kind": kind,
        }
        if kind == "adoptive":
            link["adoption_event_id"] = event["id"]
        claim(child, "relationship.parent_child", link, grade="B")
    claim(4, "person.name", {"raw": "Lumen Opalreed", "name_kind": "birth"})
    claim(4, "person.name", {"raw": "Glimmer Quill", "name_kind": "familiar"})
    claim(
        6,
        "relationship.spouse",
        {"other_id": rid("person", 12), "raw": "Married"},
        grade="B",
    )
    claim(
        9,
        "relationship.spouse",
        {"other_id": rid("person", 13), "raw": "Possible marriage"},
        grade="D",
        status="candidate",
    )
    claim(5, "relationship.guardian", {"other_id": rid("person", 11), "raw": "Ward"})
    claim(4, "person.name", {"raw": "The Ember Scribe"}, visibility="family")
    claim(
        6,
        "relationship.spouse",
        {"other_id": rid("person", 14), "raw": "Married"},
        grade="B",
        visibility="family",
    )
    for n in (91, 92, 93):
        event = claim(n, "event.recorded_property", {"date": {"raw": "1842"}})
        row(
            "narrative",
            n,
            title="Kiln estate ledger",
            subjects=[rid("person", n)],
            assertions=[event["id"]],
            body="Listed at the kiln auction [[" + event["id"] + "]].",
        )

    # Same IDs as the base records deliberately replace only the staged fields.
    curation = claim(4, "person.name", {"raw": "Steward of the East Kiln"})
    write_record(
        root / "names",
        {
            **curation,
            "value": {
                "raw": "Lumen Amberwheel",
                "name_kind": "married",
                "preferred_display": True,
                "context": "Workshop steward",
            },
        },
    )
    added_name = claim(5, "person.name", {"raw": "Tern Lattice"})
    write_record(root / "names", {**added_name, "value": {"raw": "Tern Gleamshuttle"}})
    write_record(
        root / "sources",
        {
            **citation,
            "locations": [
                {
                    "label": label,
                    "kind": "external_original",
                    "url": f"https://example.org/folio/{n}",
                }
                for n, label in enumerate(
                    ("Roll face", "Roll reverse", "Seal detail", "Margin note"), 1
                )
            ],
        },
    )

    # A family-consented living person and a real restricted synthetic byte fixture
    # are used by the outage harness to distinguish withheld and unknown resources.
    person = row("person", 95, visibility="family")
    name = claim(95, "person.name", {"raw": "Violet Candlewick"}, visibility="family")
    row(
        "consent",
        1,
        visibility="admin",
        person_id=person["id"],
        decision="grant",
        audience="family",
        recorded_at="2026-01-01",
        method="Invented workshop permission",
        scope=[person["id"], name["id"]],
    )
    data = b"Glassharbor workshop slip\n"
    (root / "assets").mkdir()
    (root / "assets/slip.txt").write_bytes(data)
    row(
        "asset",
        1,
        visibility="family",
        source_id=rid("source", 1),
        label="Workshop slip",
        rights_basis="unknown",
        redistribution="restricted",
        media_type="text/plain",
        storage_key="slip.txt",
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
