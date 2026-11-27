"""Versioned serving documents. The private acceptance boundary supplies the expected pins."""

from __future__ import annotations

import hashlib
import json
import re
from functools import cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from .model import canonical_bytes
from .publication_inputs import strict_json

BASELINE = "baseline-v1"
ENHANCED = "profile-v1"
PROFILES = {BASELINE: (), ENHANCED: ("favorite-color", "profile-photo")}
RELEASED_BASELINE = "bff431ac4bba664f2e2b7250c995150fbca4d153"
FEATURES = {"person.favorite_color", "person.photo"}
COLLECTIONS = (
    "people",
    "personas",
    "assertions",
    "history",
    "alternatives",
    "places",
    "narratives",
    "assets",
    "sources",
    "citations",
)
SCHEMAS = ("entity", "assertion-values", "projection")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@cache
def schema_bytes(name: str) -> bytes:
    return files("genealogy").joinpath("schemas", name + ".schema.json").read_bytes()


def contract_digest() -> str:
    return digest(canonical_bytes({name: digest(schema_bytes(name)) for name in SCHEMAS}))


@cache
def output_validator() -> Draft202012Validator:
    registry: Registry[Any] = Registry()
    for name in SCHEMAS:
        resource = Resource.from_contents(
            json.loads(schema_bytes(name)), default_specification=DRAFT202012
        )
        registry = registry.with_resource("urn:genealogy:" + name, resource)
    return Draft202012Validator(
        json.loads(schema_bytes("projection")), registry=registry, format_checker=FormatChecker()
    )


def validate_output(doc: dict[str, Any], audience: str, profile: str) -> None:
    if (
        type(doc.get("schema_version")) is not int
        or profile not in PROFILES
        or doc.get("audience") != audience
    ):
        raise ValueError("unsupported projection target")
    if profile == ENHANCED:
        if doc.get("projection_profile") != profile or doc.get("required_capabilities") != list(
            PROFILES[profile]
        ):
            raise ValueError("projection capability mismatch")
    elif "projection_profile" in doc or "required_capabilities" in doc:
        raise ValueError("baseline envelope mismatch")
    if next(output_validator().iter_errors(doc), None) is not None:
        raise ValueError("invalid projection row or envelope")
    by_id = {row["id"]: row for category in COLLECTIONS for row in doc[category]}
    if len(by_id) != sum(len(doc[c]) for c in COLLECTIONS):
        raise ValueError("duplicate serving identity")
    for category in COLLECTIONS:
        for row in doc[category]:
            refs = [
                row[k]
                for k in ("source_id", "subject_id", "citation_id", "display_name_assertion_id")
                if k in row
            ]
            refs.extend(v for k, v in row.get("value", {}).items() if k.endswith("_id"))
            for k in ("subjects", "assertions", "superseded_by"):
                refs.extend(row.get(k, []))
            refs.extend(e["citation_id"] for e in row.get("evidence", []))
            refs.extend(loc["asset_id"] for loc in row.get("locations", []) if "asset_id" in loc)
            for indicator in row.get("indicators", []):
                refs.extend(indicator["assertion_ids"])
            if any(ref not in by_id for ref in refs):
                raise ValueError("unclosed projection reference")
            selected = row.get("display_name_assertion_id")
            if selected and (
                by_id[selected].get("assertion_type") != "person.name"
                or by_id[selected]["subject_id"] != row["id"]
                or by_id[selected]["status"] != "accepted"
            ):
                raise ValueError("invalid display-name reference")
            if "portrait_attribution" in row and (
                not row["bytes_available"] or row["media_type"] != "image/png"
            ):
                raise ValueError("invalid portrait asset")
            if row.get("assertion_type") in FEATURES:
                raise ValueError("private display assertion in serving graph")
            if (
                profile == BASELINE
                and {"favorite_color", "profile_photo", "portrait_attribution"} & row.keys()
            ):
                raise ValueError("feature in baseline projection")
            if "profile_photo" in row:
                target = by_id.get(row["profile_photo"]["asset_id"], {})
                if not target.get("bytes_available") or not target.get("portrait_attribution"):
                    raise ValueError("unavailable portrait")


def descriptor(
    public: dict[str, Any],
    family: dict[str, Any],
    profile: str,
    target_runtime: str,
    inputs_digest: str,
) -> dict[str, Any]:
    if profile not in PROFILES or not re.fullmatch("[0-9a-f]{40}", target_runtime):
        raise ValueError("unsupported projection target")
    if target_runtime == RELEASED_BASELINE and profile != BASELINE:
        raise ValueError("released reader only supports baseline-v1")
    for audience, doc in (("public", public), ("family", family)):
        validate_output(doc, audience, profile)
    return {
        "version": 1,
        "contract_sha256": contract_digest(),
        "projection_profile": profile,
        "required_capabilities": list(PROFILES[profile]),
        "target_runtime": target_runtime,
        "publication_inputs_digest": inputs_digest,
        "documents": {
            "public": digest(canonical_bytes(public)),
            "family": digest(canonical_bytes(family)),
        },
    }


def verify_descriptor(raw: bytes, expected_digest: str, expected: dict[str, Any]) -> None:
    if (
        digest(raw) != expected_digest
        or raw != canonical_bytes(expected)
        or strict_json(raw) != expected
    ):
        raise ValueError("projection descriptor does not match accepted target and documents")
