"""Select supported profile fields and remove feature-only catalog dependencies before closure."""

from __future__ import annotations

from typing import Any

from .model import VISIBILITY_RANK
from .projection_contract import ENHANCED, FEATURES

Record = dict[str, Any]


def select_fields(
    records: list[Record],
    visible: dict[str, Record],
    audience: str,
    profile: str,
    superseded: set[str],
    verified_portraits: frozenset[tuple[str, str]],
) -> dict[str, Record]:
    rights: dict[tuple[str, str], list[Record]] = {}
    for r in records:
        if (
            r["type"] == "consent"
            and "person_id" in r
            and not r["retired"]
            and r["id"] not in superseded
        ):
            for target in r["scope"]:
                rights.setdefault((r["person_id"], target), []).append(r)

    def granted(person: str, asset: str) -> bool:
        grants = rights.get((person, asset), [])
        return (
            bool(grants)
            and all(g["decision"] == "grant" for g in grants)
            and any(VISIBILITY_RANK[g["audience"]] >= VISIBILITY_RANK[audience] for g in grants)
        )

    feature_citations = {
        e["citation_id"]
        for r in records
        if r.get("assertion_type") in FEATURES
        for e in r["evidence"]
    }
    feature_sources = {
        r["source_id"] for r in records if "portrait" in r or r["id"] in feature_citations
    }
    fields: dict[str, Record] = {}
    retained_assets: set[str] = set()
    retained_evidence: set[str] = set()
    for claim in records:
        typ = claim.get("assertion_type")
        if typ not in FEATURES:
            continue
        # Display assertions are never standalone claims, catalog entries or search roots.
        current = visible.pop(claim["id"], None)
        subject = claim["subject_id"]
        if (
            not current
            or profile != ENHANCED
            or subject not in visible
            or claim["id"] in superseded
            or claim["status"] != "accepted"
        ):
            continue
        evidence = [
            visible[e["citation_id"]]
            for e in claim["evidence"]
            if e["stance"] == "supports"
            and e["citation_id"] in visible
            and visible[e["citation_id"]]["source_id"] in visible
        ]
        if not evidence:
            continue
        if typ == "person.favorite_color":
            fields.setdefault(subject, {})["favorite_color"] = claim["value"]["text"]
        else:
            aid = claim["value"]["asset_id"]
            asset = visible.get(aid, {})
            portrait = asset.get("portrait", {})
            source = visible.get(asset.get("source_id", ""), {})
            subjects = portrait.get("depicted_people", [])
            if (
                (aid, asset.get("sha256", "")) not in verified_portraits
                or subject not in subjects
                or not all(p in visible and granted(p, aid) for p in subjects)
                or not all(
                    r.get("redistribution") == "permitted"
                    and r.get("rights_basis") in {"public_domain", "permission", "license"}
                    for r in (asset, source)
                )
                or not any(c["source_id"] == source.get("id") for c in evidence)
            ):
                continue
            fields.setdefault(subject, {})["profile_photo"] = {"asset_id": aid}
            retained_assets.add(aid)
        retained_evidence.update(c["id"] for c in evidence)

    for rid, r in list(visible.items()):
        if "portrait" in r and rid not in retained_assets:
            del visible[rid]
    # Only visible baseline claims/personas can independently support a marked
    # citation. Source-only portrait metadata never becomes an independent root.
    for r in visible.values():
        retained_evidence.update(e["citation_id"] for e in r.get("evidence", []))
        if r["type"] == "persona":
            retained_evidence.add(r["citation_id"])
    for rid, r in list(visible.items()):
        if (
            r["type"] == "citation"
            and (r.get("profile_only") or rid in feature_citations)
            and rid not in retained_evidence
        ):
            del visible[rid]
    sources = {r["source_id"] for r in visible.values() if "source_id" in r}
    for rid, r in list(visible.items()):
        if (
            r["type"] == "source"
            and (r.get("profile_only") or rid in feature_sources)
            and rid not in sources
        ):
            del visible[rid]
    return fields
