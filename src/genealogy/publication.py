from __future__ import annotations

import hashlib
import json
from typing import Any

from .classification import classify_people
from .indicators import annotate_indicators
from .model import VISIBILITY_RANK, canonical_bytes
from .name_policy import NamePolicy, fold
from .names import select_name
from .policy import content_refs, effective_visibility, superseded_ids
from .publication_inputs import PublicationInputs


def compile_publication(
    records: list[dict[str, Any]], audience: str, *, inputs: PublicationInputs
) -> dict[str, Any]:
    """Compile a closed audience graph using only explicit validated inputs."""
    records = sorted(records, key=lambda r: r["id"])
    return _compile_publication(
        records, audience, effective_visibility(records, inputs.build_year), inputs
    )


def compile_publications(
    records: list[dict[str, Any]], *, inputs: PublicationInputs
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Compile both audiences and decisions from the same immutable input snapshot."""
    records = sorted(records, key=lambda r: r["id"])
    effective = effective_visibility(records, inputs.build_year)
    return (
        _compile_publication(records, "public", effective, inputs),
        _compile_publication(records, "family", effective, inputs),
        effective,
    )


def _compile_publication(
    records: list[dict[str, Any]],
    audience: str,
    effective: dict[str, str],
    inputs: PublicationInputs,
) -> dict[str, Any]:
    name_policy = inputs.name_policy
    if audience not in {"public", "family"}:
        raise ValueError("audience must be public or family")
    threshold = VISIBILITY_RANK[audience]
    visible = {
        r["id"]: r
        for r in records
        if VISIBILITY_RANK[effective[r["id"]]] >= threshold
        and not r["retired"]
        and r["type"] not in {"consent", "research_question"}
    }
    # Fixed point: dropping an endpoint can invalidate another endpoint or narrative.
    while True:
        remove = {
            rid for rid, r in visible.items() if any(ref not in visible for ref in content_refs(r))
        }
        if not remove:
            break
        for rid in remove:
            del visible[rid]
    superseded = superseded_ids(records)
    reverse_supersession: dict[str, list[str]] = {}
    conflict_members: dict[str, list[str]] = {}
    for r in visible.values():
        if r.get("supersedes"):
            reverse_supersession.setdefault(r["supersedes"], []).append(r["id"])
        if r.get("conflict_group") and (r["status"] != "accepted" or r["id"] in superseded):
            conflict_members.setdefault(r["conflict_group"], []).append(r["id"])
    # Group only visible alternatives/history. Canonical labels can contain names,
    # and a singleton group could reveal a hidden counterpart. Neither is copied.
    conflict_groups = {
        rid: "cfg_" + hashlib.sha256(canonical_bytes(sorted(members))).hexdigest()
        for members in conflict_members.values()
        if len(members) > 1
        for rid in members
    }
    output: dict[str, Any] = {
        "schema_version": 1,
        "audience": audience,
        "people": [],
        "personas": [],
        "assertions": [],
        "history": [],
        "alternatives": [],
        "places": [],
        "narratives": [],
        "assets": [],
        "sources": [],
        "citations": [],
    }
    for r in visible.values():
        kind = r["type"]
        item: dict[str, Any] = {"id": r["id"]}
        if kind == "assertion":
            item.update(
                {k: r[k] for k in ("assertion_type", "subject_id", "value", "status", "origin")}
            )
            if r.get("grade"):
                item["grade"] = r["grade"]
            evidence = [e for e in r["evidence"] if e["citation_id"] in visible]
            if evidence:
                item["evidence"] = evidence
            # Research reasoning/dependencies may quote restricted evidence: never copy.
            newer = reverse_supersession.get(r["id"], [])
            if newer:
                item["superseded_by"] = newer
            destination = (
                "history"
                if r["id"] in superseded or r["status"] in {"rejected", "corrected"}
                else "assertions"
                if r["status"] == "accepted"
                else "alternatives"
            )
            if r["id"] in conflict_groups:
                item["conflict_group"] = conflict_groups[r["id"]]
        elif kind == "person":
            destination = "people"
        elif kind == "persona":
            item.update({k: r[k] for k in ("raw_name", "role", "citation_id")})
            destination = "personas"
        elif kind == "place":
            item["label"] = r["label"]
            destination = "places"
        elif kind == "narrative":
            item.update({k: r[k] for k in ("title", "body", "subjects", "assertions")})
            destination = "narratives"
        elif kind == "asset":
            item.update({k: r[k] for k in ("label", "media_type", "source_id")})
            item["bytes_available"] = r["redistribution"] == "permitted"
            # No archive paths or hashes for unavailable bytes.
            if item["bytes_available"]:
                item["sha256"] = r["sha256"]
            destination = "assets"
        elif kind == "source":
            item.update({k: r[k] for k in ("title", "source_kind")})
            if (
                "reader_description" in r
                and VISIBILITY_RANK[r.get("reader_description_visibility", r["visibility"])]
                >= threshold
            ):
                item["reader_description"] = r["reader_description"]
            destination = "sources"
        elif kind == "citation":
            item.update({k: r[k] for k in ("source_id", "locator", "inspection")})
            if VISIBILITY_RANK[r["transcription_visibility"]] >= threshold:
                item["transcription"] = r["transcription"]
            if "locations" in r:
                item["locations"] = [
                    dict(location)
                    for location in r["locations"]
                    if location["kind"] != "hosted_copy"
                    or (
                        location["asset_id"] in visible
                        and visible[location["asset_id"]].get("redistribution") == "permitted"
                        and visible[location["asset_id"]].get("source_id") == r["source_id"]
                    )
                ]
            destination = "citations"
        else:
            continue
        output[destination].append(item)
    # Select once after audience closure and current/history classification. Reference
    # the existing scanned name field instead of duplicating names (and exemptions).
    names_by_subject: dict[str, list[dict[str, Any]]] = {}
    for assertion in output["assertions"]:
        if assertion["assertion_type"] == "person.name":
            names_by_subject.setdefault(assertion["subject_id"], []).append(assertion)
    for person in output["people"]:
        selected = select_name(names_by_subject.get(person["id"], []))
        if selected:
            person["display_name_assertion_id"] = selected["id"]
    classify_people(output, roots=inputs.family_roots)
    annotate_indicators(output, records, inputs=inputs)
    # Scan both declared restrictions and effective restrictions (living/consent,
    # retirement, graph closure). No exception may exempt an opaque record ID.
    forbidden_ids = {r["id"] for r in records if r["id"] not in visible}
    forbidden_names: set[str] = set()
    for r in records:
        if r["type"] == "persona" and r["id"] not in visible and r["raw_name"]:
            forbidden_names.add(r["raw_name"])
        if r.get("assertion_type") == "person.name" and (
            r["id"] not in visible or r["subject_id"] not in visible
        ):
            forbidden_names.update(
                x for k, x in r["value"].items() if k in {"raw", "normalized"} and x
            )
    assert_no_text_leaks(output, forbidden_ids)
    (name_policy or NamePolicy()).scan(output, forbidden_names)
    return output


def assert_no_text_leaks(document: dict[str, Any], forbidden: set[str]) -> None:
    """Scan decoded text, including labels, Markdown, metadata and future UI keys."""

    text = fold(json.dumps(document, ensure_ascii=False, sort_keys=True))
    leaked = sorted(name for name in forbidden if name and fold(name) in text)
    if leaked:
        raise ValueError(f"publication text leak: {', '.join(leaked)}")
