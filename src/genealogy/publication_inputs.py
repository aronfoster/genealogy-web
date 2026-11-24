"""Validated, byte-bound publication inputs. Integrity is not publication authority."""

from __future__ import annotations

import hashlib
import json
import math
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from .name_policy import NamePolicy

COMPONENTS = ("name_policy", "family_roots", "historical_name_rules", "reviewed_name_contexts")
MAX_INPUT_BYTES = 4 * 1024 * 1024


def strict_json(raw: bytes) -> Any:
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("publication input is empty or exceeds the size limit")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate publication input key")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError("non-finite publication input number")

    def finite(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("non-finite publication input number")
        return result

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=finite,
        )
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("invalid publication input JSON") from exc


def canonical_manifest(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(document), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def validate_component(kind: str, document: Any) -> None:
    schema = json.loads(
        files("genealogy").joinpath("schemas/publication-inputs.schema.json").read_bytes()
    )
    validator = Draft202012Validator(schema["$defs"][kind])
    if not validator.is_valid(document):
        # Do not echo private fields, values or paths into generic diagnostics.
        raise ValueError(f"invalid {kind} schema")


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return value


def read_input(path: Path) -> bytes:
    # Explicit regular data files only; no source discovery or executable loaders.
    path = path.absolute()
    if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("publication input path must not traverse symlinks or parents")
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o111:
            raise ValueError("publication input must be a non-executable regular file")
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise ValueError("publication input is missing or unreadable") from exc
    strict_json(raw)
    return raw


def publication_inputs_digest(manifest: Mapping[str, Any]) -> str:
    if (
        type(manifest.get("build_year")) is not int
        or type(manifest.get("schema_version")) is not int
    ):
        raise ValueError("invalid manifest schema")
    validate_component("manifest", dict(manifest))
    return hashlib.sha256(
        b"genealogy-publication-inputs/v1\0" + canonical_manifest(manifest)
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class PublicationInputs:
    """Own immutable bytes and derive every semantic field from those same bytes.

    Empty inputs require a declared synthetic scenario or a caller-accepted private
    tuple. The digest binds that assertion; it does not verify independent approval.
    """

    build_year: int
    name_policy_bytes: bytes = field(repr=False)
    family_roots_bytes: bytes = field(repr=False)
    historical_name_rules_bytes: bytes = field(repr=False)
    reviewed_name_contexts_bytes: bytes = field(repr=False)
    profile: str = "private-metadata"
    accepted_empty_digest: str | None = None
    name_policy: NamePolicy = field(init=False)
    family_roots: frozenset[str] = field(init=False)
    historical_name_rules: Mapping[str, Any] = field(init=False, repr=False)
    reviewed_name_contexts: Mapping[str, Any] = field(init=False, repr=False)
    manifest: Mapping[str, Any] = field(init=False)
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.profile not in {"synthetic", "private-metadata", "private-bytes"}:
            raise ValueError("invalid publication input profile")
        documents = {}
        if type(self.build_year) is not int:
            raise ValueError("build_year must be an integer")
        manifest: dict[str, Any] = {"schema_version": 1, "build_year": self.build_year}
        for key in COMPONENTS:
            raw = getattr(self, key + "_bytes")
            if type(raw) is not bytes:
                raise ValueError("publication components must be immutable bytes")
            documents[key] = strict_json(raw)
            manifest[key + "_sha256"] = hashlib.sha256(raw).hexdigest()
            if key != "name_policy":
                validate_component(key, documents[key])
        policy = NamePolicy.from_document(documents["name_policy"])
        digest = publication_inputs_digest(manifest)
        if self.accepted_empty_digest is not None and self.accepted_empty_digest != digest:
            raise ValueError("accepted publication input digest mismatch")
        if (
            self.profile != "synthetic"
            and self.accepted_empty_digest is None
            and (
                not policy.occurrences
                or not documents["family_roots"]["roots"]
                or not documents["reviewed_name_contexts"]["contexts"]
                or not documents["historical_name_rules"]["cohorts"]
                or not documents["historical_name_rules"]["stable_historical_names"]
            )
        ):
            raise ValueError("empty private publication inputs require an accepted tuple")
        object.__setattr__(self, "name_policy", policy)
        object.__setattr__(self, "family_roots", frozenset(documents["family_roots"]["roots"]))
        object.__setattr__(
            self, "historical_name_rules", freeze(documents["historical_name_rules"])
        )
        object.__setattr__(
            self, "reviewed_name_contexts", freeze(documents["reviewed_name_contexts"]["contexts"])
        )
        object.__setattr__(self, "manifest", freeze(manifest))
        object.__setattr__(self, "digest", digest)

    @classmethod
    def from_files(
        cls,
        *,
        build_year: int,
        name_policy: Path,
        family_roots: Path,
        historical_name_rules: Path,
        reviewed_name_contexts: Path,
        profile: str = "private-metadata",
        expected_digest: str | None = None,
        accepted_empty_digest: str | None = None,
    ) -> PublicationInputs:
        result = cls(
            build_year,
            read_input(name_policy),
            read_input(family_roots),
            read_input(historical_name_rules),
            read_input(reviewed_name_contexts),
            profile=profile,
            accepted_empty_digest=accepted_empty_digest,
        )
        if expected_digest is not None and result.digest != expected_digest:
            raise ValueError("publication input digest mismatch")
        return result

    def metadata(self) -> dict[str, Any]:
        """Separate versioned metadata; never appended to legacy projection bytes."""
        return {
            "publication_inputs": dict(self.manifest),
            "publication_inputs_digest": self.digest,
            "profile": self.profile,
            "accepted_empty": self.accepted_empty_digest is not None,
        }
