"""Deterministic publication decisions; no consent records reach serving state."""

from __future__ import annotations

import re
from typing import Any

from .model import VISIBILITY_RANK

NARRATIVE_REF = re.compile(r"\[\[(asn_[0-9a-f-]+)\]\]")


def superseded_ids(records: list[dict[str, Any]]) -> set[str]:
    return {r["supersedes"] for r in records if not r["retired"] and r.get("supersedes")}


def content_refs(record: dict[str, Any]) -> list[str]:
    """References that survive publication, unlike private research/evidence links."""
    kind = record["type"]
    if kind == "assertion":
        return [record["subject_id"]] + [v for k, v in record["value"].items() if k.endswith("_id")]
    if kind in {"citation", "asset"}:
        return [record["source_id"]]
    if kind == "persona":
        return [record["citation_id"]]
    if kind == "narrative":
        return sorted(
            set(record["subjects"] + record["assertions"] + NARRATIVE_REF.findall(record["body"]))
        )
    return []


def living_people(records: list[dict[str, Any]], build_year: int) -> set[str]:
    superseded = superseded_ids(records)
    deceased: set[str] = set()
    for r in records:
        if r["type"] != "assertion" or r["retired"] or r["id"] in superseded:
            continue
        if r["status"] != "accepted" or not r["subject_id"].startswith("per_"):
            continue
        if r["assertion_type"] == "person.death":
            deceased.add(r["subject_id"])
        elif r["assertion_type"] == "person.birth":
            date = r["value"]["date"]
            normalized = date.get("normalized")
            # An approximate/after/unknown date does not prove the person is over 110.
            if normalized and date.get("precision", "exact") in {"exact", "before"}:
                if int(normalized[:4]) < build_year - 110:
                    deceased.add(r["subject_id"])
    return {r["id"] for r in records if r["type"] == "person"} - deceased


def effective_visibility(records: list[dict[str, Any]], build_year: int) -> dict[str, str]:
    superseded = superseded_ids(records)
    consents = [
        r
        for r in records
        if r["type"] == "consent" and not r["retired"] and r["id"] not in superseded
    ]
    living = living_people(records, build_year)
    indexed = {r["id"]: r for r in records}
    # Build the same direct-identity policy once, rather than rescanning the corpus
    # for every record. Each entry holds living people at that identity's endpoints.
    living_by_endpoint: dict[str, set[str]] = {}
    for identity in records:
        if (
            identity["type"] == "assertion"
            and identity["assertion_type"] == "identity.same_person"
            and identity["status"] == "accepted"
            and not identity["retired"]
            and identity["id"] not in superseded
        ):
            endpoints = {identity["subject_id"], identity["value"]["other_id"]}
            for endpoint in endpoints:
                living_by_endpoint.setdefault(endpoint, set()).update(endpoints & living)
    person_consents: dict[tuple[str, str], list[dict[str, Any]]] = {}
    informant_consents: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for consent in consents:
        for target in consent["scope"]:
            if "person_id" in consent:
                person_consents.setdefault((consent["person_id"], target), []).append(consent)
            if "informant_source_id" in consent:
                informant_consents.setdefault((consent["informant_source_id"], target), []).append(
                    consent
                )
    result: dict[str, str] = {}
    for r in records:
        visibility = r["visibility"]
        refs = set(content_refs(r))
        subjects = ({r["id"]} if r["type"] == "person" else refs) & living
        for endpoint in refs | {r["id"]}:
            subjects |= living_by_endpoint.get(endpoint, set())
        for person in subjects:
            matching = person_consents.get((person, r["id"]), [])
            if not any(
                c["decision"] == "grant"
                and VISIBILITY_RANK[c["audience"]] >= VISIBILITY_RANK[visibility]
                for c in matching
            ) or any(c["decision"] != "grant" for c in matching):
                visibility = "withheld"
        if subjects and visibility == "public":
            visibility = "family"
        # Publishing testimony words requires separate informant permission.
        if r["type"] in {"persona", "citation"}:
            citation = r if r["type"] == "citation" else indexed.get(r["citation_id"], {})
            source = indexed.get(citation.get("source_id"), {})
            if source.get("source_kind") == "oral_or_family_testimony":
                matching = informant_consents.get((source["id"], r["id"]), [])
                if not any(
                    c["decision"] == "grant"
                    and VISIBILITY_RANK[c["audience"]] >= VISIBILITY_RANK[visibility]
                    for c in matching
                ) or any(c["decision"] != "grant" for c in matching):
                    visibility = "withheld"
        result[r["id"]] = visibility
    return result
