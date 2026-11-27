from __future__ import annotations

import hashlib
import json
import re
import struct
import time
import uuid
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker

from . import SCHEMA_VERSION
from .evidence_urls import safe_evidence_url
from .model import PREFIXES, VISIBILITY_RANK, RecordReadError, canonical_bytes, read_records
from .policy import NARRATIVE_REF, content_refs, superseded_ids
from .portrait import MAX_BYTES, safe_png
from .projection_contract import FEATURES


@dataclass(frozen=True)
class Violation:
    rule_id: str
    path: str
    entity_id: str | None = None
    referenced_id: str | None = None
    detail: str = ""

    def __str__(self) -> str:
        return ": ".join(
            str(x)
            for x in (self.rule_id, self.path, self.entity_id, self.referenced_id, self.detail)
            if x
        )


class CorpusInvalid(Exception):
    def __init__(self, violations: Iterable[Violation]):
        self.violations = sorted(violations, key=str)
        super().__init__("\n".join(map(str, self.violations)))


class CorpusValidator:
    def __init__(self, schema_dir: Path | None = None) -> None:
        directory = schema_dir or files("genealogy").joinpath("schemas")
        self.entity_schema = json.loads(directory.joinpath("entity.schema.json").read_text())
        self.value_schema = json.loads(
            directory.joinpath("assertion-values.schema.json").read_text()
        )
        self.verified_portraits: dict[tuple[str, str], tuple[int, int]] = {}
        self.unverified_assets: list[str] = []
        self.warnings: list[Violation] = []
        Draft202012Validator.check_schema(self.entity_schema)
        Draft202012Validator.check_schema(self.value_schema)

    def validate(
        self, root: Path, asset_root: Path | None = None, *, require_bytes: bool = False
    ) -> list[dict[str, Any]]:
        self.verified_portraits.clear()
        self.unverified_assets = []
        self.warnings = []
        try:
            pairs = read_records(root)
        except RecordReadError as exc:
            raise CorpusInvalid(
                [Violation("schema.invalid_json", str(exc.path), detail=str(exc))]
            ) from exc
        errors: list[Violation] = []
        indexed: dict[str, dict[str, Any]] = {}
        paths: dict[str, str] = {}
        versions: set[str] = set()
        seen: set[str] = set()
        for path, r in pairs:
            rid = r.get("id") if isinstance(r.get("id"), str) else None
            rel = str(path.relative_to(root))
            versions.add(str(r.get("schema_version")))
            if rid:
                if rid in seen:
                    errors.append(Violation("id.duplicate", rel, rid, rid))
                seen.add(rid)
                paths[rid] = rel
            schema_errors = list(
                Draft202012Validator(
                    self.entity_schema, format_checker=FormatChecker()
                ).iter_errors(r)
            )
            for error in schema_errors:
                # Leaf errors are substantially more useful than dumping the whole record.
                errors.append(
                    Violation(
                        "schema.invalid",
                        rel,
                        rid,
                        detail=error.message
                        if not error.context
                        else "; ".join(
                            e.message for e in error.context if list(e.path) and e.path[0] != "type"
                        ),
                    )
                )
            value_errors = []
            if r.get("type") == "assertion":
                value_errors = list(
                    Draft202012Validator(
                        self.value_schema, format_checker=FormatChecker()
                    ).iter_errors(r)
                )
                if value_errors:
                    errors.append(
                        Violation(
                            "assertion.value_schema",
                            rel,
                            rid,
                            detail="invalid controlled assertion value",
                        )
                    )
            if rid:
                prefix = PREFIXES.get(str(r.get("type")))
                if not prefix or not rid.startswith(prefix + "_"):
                    errors.append(Violation("id.wrong_prefix", rel, rid))
                else:
                    try:
                        u = uuid.UUID(rid.split("_", 1)[1])
                        if (
                            u.version != 7
                            or u.variant != uuid.RFC_4122
                            or str(u) != rid.split("_", 1)[1]
                        ):
                            raise ValueError
                        if u.int >> 80 > time.time_ns() // 1_000_000 + 300_000:
                            errors.append(Violation("id.future", rel, rid))
                    except ValueError:
                        errors.append(Violation("id.malformed", rel, rid))
            if (
                len(path.relative_to(root).parts) != 2
                or path.parent.name != r.get("type")
                or path.stem != rid
                or path.is_symlink()
            ):
                errors.append(Violation("file.identity_mismatch", rel, rid))
            if canonical_bytes(r) != path.read_bytes():
                errors.append(Violation("serialization.noncanonical", rel, rid))
            if not schema_errors and not value_errors and rid:
                indexed[rid] = r
        if versions != {str(SCHEMA_VERSION)}:
            errors.append(
                Violation("schema.version_set", ".", detail=f"versions={sorted(versions)}")
            )
        # Schema-invalid data must never reach semantic code expecting typed fields.
        if errors:
            raise CorpusInvalid(errors)

        def fail(rule: str, r: dict[str, Any], ref: str | None = None, detail: str = "") -> None:
            errors.append(Violation(rule, paths[r["id"]], r["id"], ref, detail))

        def ref(r: dict[str, Any], target: str, prefixes: tuple[str, ...]) -> None:
            if not target.startswith(prefixes):
                fail("ref.wrong_prefix", r, target)
            elif target not in indexed:
                fail("ref.dangling", r, target)

        records = list(indexed.values())
        portrait_hashes = {r["sha256"] for r in records if "portrait" in r}
        portrait_keys = {r["storage_key"] for r in records if "portrait" in r}
        asset_hashes: set[str] = set()
        asset_keys: set[str] = set()
        for r in records:
            kind = r["type"]
            if kind == "source" and "reader_description" in r:
                if not r["reader_description"].strip():
                    fail("source.empty_description", r)
                if (
                    VISIBILITY_RANK[r.get("reader_description_visibility", r["visibility"])]
                    > VISIBILITY_RANK[r["visibility"]]
                ):
                    fail("privacy.description_broader", r)
            if kind == "citation":
                for location in r.get("locations", []):
                    if not location["label"].strip() or re.search(
                        r"[a-z][a-z0-9+.-]*://", location["label"], re.I
                    ):
                        fail("citation.invalid_location_label", r)
                    if "url" in location and not safe_evidence_url(location["url"]):
                        fail("citation.unsafe_location_url", r)
                    if location["kind"] == "hosted_copy":
                        asset_id = location["asset_id"]
                        ref(r, asset_id, ("ast_",))
                        asset = indexed.get(asset_id, {})
                        if (
                            asset.get("source_id") != r["source_id"]
                            or asset.get("redistribution") != "permitted"
                            or asset.get("retired")
                        ):
                            fail("citation.invalid_hosted_copy", r, asset_id)
            if r.get("tombstone_of"):
                ref(r, r["tombstone_of"], (PREFIXES[kind] + "_",))
            if r.get("supersedes"):
                ref(r, r["supersedes"], (PREFIXES[kind] + "_",))
            if kind == "persona":
                ref(r, r["citation_id"], ("cit_",))
            elif kind in {"citation", "asset"}:
                ref(r, r["source_id"], ("src_",))
                if (
                    kind == "citation"
                    and VISIBILITY_RANK[r["transcription_visibility"]]
                    > VISIBILITY_RANK[r["visibility"]]
                ):
                    fail("privacy.transcription_broader", r)
            if kind == "asset":
                if r["visibility"] == "public" and r["redistribution"] != "permitted":
                    fail("rights.public_asset", r)
                if (r["sha256"] in portrait_hashes and r["sha256"] in asset_hashes) or (
                    r["storage_key"] in portrait_keys and r["storage_key"] in asset_keys
                ):
                    fail("portrait.ambiguous_bytes", r)
                asset_hashes.add(r["sha256"])
                asset_keys.add(r["storage_key"])
                if "portrait" in r:
                    for person in r["portrait"]["depicted_people"]:
                        ref(r, person, ("per_",))
                    if r["media_type"] != "image/png" or r["byte_size"] > MAX_BYTES:
                        fail("portrait.invalid_derivative", r)
                key = Path(r["storage_key"])
                if key.is_absolute() or ".." in key.parts:
                    fail("asset.unsafe_path", r)
                elif asset_root is not None:
                    blob = asset_root / key
                    if not blob.resolve().is_relative_to(asset_root.resolve()):
                        fail("asset.unsafe_path", r)
                    elif blob.is_file():
                        data = blob.read_bytes()
                        if (
                            len(data) != r["byte_size"]
                            or hashlib.sha256(data).hexdigest() != r["sha256"]
                        ):
                            fail("asset.hash_mismatch", r)
                        elif "portrait" in r:
                            if safe_png(data):
                                self.verified_portraits[r["id"], r["sha256"]] = struct.unpack_from(
                                    ">II", data, 16
                                )
                            else:
                                fail("portrait.invalid_derivative", r)
                    else:
                        fail("asset.hash_mismatch", r, detail="missing bytes")
                elif require_bytes:
                    fail("asset.bytes_required", r)
                else:
                    self.unverified_assets.append(r["id"])
            elif kind == "consent":
                if r["visibility"] not in {"admin", "withheld"}:
                    fail("privacy.governance", r)
                if not r.get("person_id") and not r.get("informant_source_id"):
                    fail("consent.subject_required", r)
                if r.get("person_id"):
                    ref(r, r["person_id"], ("per_",))
                if r.get("informant_source_id"):
                    ref(r, r["informant_source_id"], ("src_",))
                if not r["scope"]:
                    fail("consent.scope_required", r)
                for target in r["scope"]:
                    ref(r, target, tuple(p + "_" for p in PREFIXES.values()))
            elif kind == "research_question":
                for target in r["references"]:
                    ref(r, target, tuple(p + "_" for p in PREFIXES.values()))
            elif kind == "narrative":
                valid = set(NARRATIVE_REF.findall(r["body"]))
                if any(x not in valid for x in re.findall(r"\[\[([^]]+)\]\]", r["body"])):
                    fail("narrative.reference_syntax", r)
                if valid != set(r["assertions"]):
                    fail("narrative.reference_list", r)
                for target in r["subjects"]:
                    ref(r, target, ("per_",))
                for target in r["assertions"]:
                    ref(r, target, ("asn_",))
                    if (
                        target in indexed
                        and indexed[target]["retired"]
                        and not r.get("historical_references")
                    ):
                        fail("narrative.retired_reference", r, target)
            elif kind == "assertion":
                origin, typ = r["origin"], r["assertion_type"]
                if typ == "person.name":
                    value = r["value"]
                    if not value["raw"].strip():
                        fail("name.empty", r)
                    # Legacy uncurated labels remain loadable; newly structured
                    # names must separate an explicit relationship clause.
                    if any(
                        k in value for k in ("name_kind", "preferred_display", "context")
                    ) and re.search(
                        r",\s*(?:(?:(?:reported|deceased)\s+)?"
                        r"(?:father|mother|parent|child|son|daughter|wife|husband|widow|brother|sister)"
                        r"(?:-in-law)?\s+of\b|[^,]+['’]s\s+"
                        r"(?:son|daughter|child|brother|sister|husband|wife)\b)",
                        value["raw"],
                        re.I,
                    ):
                        fail("name.relationship_clause", r)
                    start = value.get("used_from", {}).get("normalized")
                    end = value.get("used_to", {}).get("normalized")
                    if start and end and start > end:
                        fail("name.usage_order", r)
                if typ == "person.living":
                    value = r["value"]
                    if value["as_of"] > value["valid_through"]:
                        fail("living.validity_order", r)
                    if int(value["valid_through"][:4]) - int(value["as_of"][:4]) > 1:
                        fail("living.revalidation_required", r)
                identity = typ.startswith("identity.")
                expected = (
                    ("psn_",)
                    if origin == "source_statement"
                    else ("per_", "psn_")
                    if identity
                    else ("src_", "cit_", "ast_")
                    if typ == "source.interpretation"
                    else ("per_",)
                )
                ref(r, r["subject_id"], expected)
                for e in r["evidence"]:
                    ref(r, e["citation_id"], ("cit_",))
                for target in r["dependencies"]:
                    ref(r, target, ("asn_",))
                support = [
                    indexed[e["citation_id"]]
                    for e in r["evidence"]
                    if e["stance"] == "supports" and e["citation_id"] in indexed
                ]
                if typ in FEATURES and (not support or not r["subject_id"].startswith("per_")):
                    fail("profile.cited_person_required", r)
                if typ == "person.photo":
                    asset = indexed.get(r["value"]["asset_id"], {})
                    if r["subject_id"] not in asset.get("portrait", {}).get("depicted_people", []):
                        fail("portrait.subject_mismatch", r)
                    if not any(c["source_id"] == asset.get("source_id") for c in support):
                        fail("portrait.source_mismatch", r)
                if origin == "source_statement" and not support:
                    fail("assertion.source_requires_citation", r)
                if origin in {"researcher_derivation", "researcher_conclusion"} and (
                    not r.get("reasoning", "").strip() or not r["dependencies"]
                ):
                    fail("assertion.derivation_incomplete", r)
                if origin == "family_testimony" and not any(
                    indexed.get(c["source_id"], {}).get("source_kind") == "oral_or_family_testimony"
                    for c in support
                ):
                    fail("assertion.testimony_source", r)
                grade = r.get("grade")
                if grade is not None and (
                    origin == "source_statement"
                    or not typ.startswith(("identity.", "relationship."))
                    or (grade == "D" and r["status"] == "accepted")
                ):
                    fail("assertion.grade_status", r)
                if r["status"] == "conflicting" and not r.get("conflict_group"):
                    fail("conflict.group_required", r)
                for key, target in r["value"].items():
                    if key.endswith("_id"):
                        allowed = (
                            ("psn_", "per_")
                            if origin == "source_statement" or identity
                            else ("per_",)
                        )
                        prefixes = (
                            ("ast_",)
                            if typ == "person.photo" and key == "asset_id"
                            else ("plc_",)
                            if key == "place_id"
                            else ("asn_",)
                            if key == "adoption_event_id"
                            else allowed
                        )
                        ref(r, target, prefixes)
                if typ == "relationship.parent_child":
                    v = r["value"]
                    if r["subject_id"] != v["child_id"] or v["parent_id"] == v["child_id"]:
                        fail("relationship.child_subject", r, v["child_id"])
                    if (
                        v["relationship_kind"] == "adoptive"
                        and r["status"] == "accepted"
                        and not v.get("adoption_event_id")
                    ):
                        fail("relationship.adoption_event_required", r)
                    if v.get("adoption_event_id") in indexed:
                        event = indexed[v["adoption_event_id"]]
                        if (
                            event.get("assertion_type") != "event.adoption"
                            or event.get("subject_id") != r["subject_id"]
                            or (r["status"] == "accepted" and event.get("status") != "accepted")
                        ):
                            fail("relationship.adoption_event_invalid", r, event["id"])
            if not r["retired"]:
                for target in sorted(set(content_refs(r))):
                    if target in indexed and indexed[target]["retired"]:
                        self.warnings.append(
                            Violation(
                                "ref.retired",
                                paths[r["id"]],
                                r["id"],
                                target,
                                "active content references a retired record; review merge/split follow-up",
                            )
                        )
            # Evidence/dependencies are intentionally exempt; content references are not.
            for target in content_refs(r):
                other = indexed.get(target)
                if (
                    other
                    and VISIBILITY_RANK[r["visibility"]] > VISIBILITY_RANK[other["visibility"]]
                ):
                    fail("privacy.reference_broader", r, target)

        if errors:
            raise CorpusInvalid(errors)
        # Graph rules only run on structurally sound references.
        for edge in ("supersedes", "tombstone_of", "dependencies"):
            done: set[str] = set()
            active: set[str] = set()

            def visit(rid: str) -> None:
                if rid in active:
                    fail(f"graph.circular_{edge}", indexed[rid], rid)
                    return
                if rid in done:
                    return
                active.add(rid)
                value = indexed[rid].get(edge, [])
                for target in [value] if isinstance(value, str) else value:
                    visit(target)
                active.remove(rid)
                done.add(rid)

            for rid in indexed:
                visit(rid)
        assertions = [r for r in records if r["type"] == "assertion"]
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in assertions:
            if r.get("conflict_group"):
                groups.setdefault(r["conflict_group"], []).append(r)
        for group, members in groups.items():
            if (
                len(members) < 2
                or len({(r["subject_id"], r["assertion_type"]) for r in members}) != 1
            ):
                fail("conflict.invalid_set", members[0], group)
        superseded = superseded_ids(records)
        current = [
            r
            for r in assertions
            if r["status"] == "accepted" and not r["retired"] and r["id"] not in superseded
        ]
        preferred_names: dict[str, str] = {}
        counts: dict[tuple[str, str], list[dict[str, Any]]] = {}
        parents: dict[str, set[str]] = {}
        parent_record: dict[str, dict[str, Any]] = {}
        closure: dict[str, str] = {}

        def representative(rid: str) -> str:
            while rid in closure:
                rid = closure[rid]
            return rid

        for r in current:
            typ = r["assertion_type"]
            if typ == "person.name" and r["value"].get("preferred_display"):
                if r["subject_id"] in preferred_names:
                    fail("name.multiple_preferred", r, preferred_names[r["subject_id"]])
                preferred_names[r["subject_id"]] = r["id"]
            if (
                typ in {"person.birth", "person.death", "person.sex", "person.living"}
                and r["origin"] != "source_statement"
            ):
                counts.setdefault((r["subject_id"], typ), []).append(r)
            if (
                typ == "relationship.parent_child"
                and r["value"]["relationship_kind"] == "biological"
            ):
                parents.setdefault(r["subject_id"], set()).add(r["value"]["parent_id"])
                parent_record[r["subject_id"]] = r
            if typ == "identity.same_person":
                a, b = representative(r["subject_id"]), representative(r["value"]["other_id"])
                if a != b:
                    closure[a] = b
        for members in counts.values():
            if len(members) > 1:
                fail("cardinality.multiple_current", members[0], members[1]["id"])
        for child, parent_ids in parents.items():
            if len(parent_ids) > 2:
                fail("cardinality.biological_parents", parent_record[child], child)
        for r in current:
            if r["assertion_type"] == "identity.not_same_person" and representative(
                r["subject_id"]
            ) == representative(r["value"]["other_id"]):
                fail("identity.contradiction", r, r["value"]["other_id"])
        if errors:
            raise CorpusInvalid(errors)
        return records
