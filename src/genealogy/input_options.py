"""Shared explicit input loading; input documents contain data, never executable loaders."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .name_policy import NamePolicy
from .publication_inputs import COMPONENTS, PublicationInputs, read_input, strict_json

PATH_FIELDS = (*COMPONENTS, "corpus", "asset_root")
FIELDS = (
    *PATH_FIELDS,
    "build_year",
    "validation_profile",
    "publication_inputs_digest",
    "accepted_empty_inputs_digest",
)
PROFILES = ("synthetic", "private-metadata", "private-bytes")
# Linux UAPI fcntl.h values; some standalone Python builds omit the names.
F_ADD_SEALS, F_GET_SEALS, SEALS = 1033, 1034, 0x0F


def publication_arguments(parser: argparse.ArgumentParser) -> None:
    """Defaults are applied after conflict detection, never before it."""
    existing = {a.dest for a in parser._actions}
    for key in FIELDS:
        if key not in existing:
            kwargs: dict[str, Any] = {}
            if key in PATH_FIELDS:
                kwargs["type"] = Path
            elif key == "build_year":
                kwargs["type"] = int
            elif key == "validation_profile":
                kwargs["choices"] = PROFILES
            parser.add_argument("--" + key.replace("_", "-"), **kwargs)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--input-metadata", type=Path)
    parser.add_argument("--publication-inputs-fd", type=int, help=argparse.SUPPRESS)


def safe_path(path: Path) -> Path:
    path = path.absolute()
    if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("input path must not traverse symlinks or parents")
    return path


def input_options(args: argparse.Namespace) -> None:
    """Resolve document-relative paths and reject every conflicting CLI value."""
    if getattr(args, "_publication_options_resolved", False):
        return
    document_path = getattr(args, "inputs", None)
    args.inputs = document_path
    if document_path is not None:
        document_path = safe_path(document_path)
        document = strict_json(read_input(document_path))
        if (
            not isinstance(document, dict)
            or type(document.get("schema_version")) is not int
            or document["schema_version"] != 1
            or set(document) - {"schema_version", *FIELDS}
        ):
            raise ValueError("invalid inputs document schema")
        for key, value in document.items():
            if key == "schema_version":
                continue
            if key == "build_year":
                valid = type(value) is int and 1 <= value <= 9999
            else:
                valid = isinstance(value, str) and bool(value)
            if not valid or (key == "validation_profile" and value not in PROFILES):
                raise ValueError("invalid inputs document field")
            if key in PATH_FIELDS:
                path = Path(value)
                value = safe_path(path if path.is_absolute() else document_path.parent / path)
            current = getattr(args, key, None)
            if current is not None:
                if key in PATH_FIELDS:
                    current = safe_path(current)
                if current != value:
                    raise ValueError(f"conflicting CLI/inputs field: {key}")
            setattr(args, key, value)
    for key in FIELDS:
        if not hasattr(args, key):
            setattr(args, key, None)
        if key in PATH_FIELDS and getattr(args, key) is not None:
            setattr(args, key, safe_path(getattr(args, key)))

    args._publication_options_resolved = True


def snapshot_bytes(inputs: PublicationInputs) -> bytes:
    # Each component stays below its original size bound. The whole envelope is
    # intentionally bounded to the same 4 MiB limit as other input documents.
    value = {
        "schema_version": 1,
        "metadata": inputs.metadata(),
        "components": {
            key: base64.b64encode(getattr(inputs, key + "_bytes")).decode("ascii")
            for key in COMPONENTS
        },
    }
    raw = json.dumps(value, sort_keys=True).encode()
    strict_json(raw)
    return raw


def snapshot_inputs(fd: int) -> PublicationInputs:
    import fcntl

    from .publication_inputs import MAX_INPUT_BYTES

    try:
        if (
            not stat.S_ISREG(os.fstat(fd).st_mode)
            or os.fstat(fd).st_mode & 0o222
            or fcntl.fcntl(fd, F_GET_SEALS) != SEALS
        ):
            raise ValueError("publication snapshot requires a sealed regular handle")
        raw = os.pread(fd, MAX_INPUT_BYTES + 1, 0)
    except OSError as exc:
        raise ValueError("publication snapshot handle is missing or unsealed") from exc
    value = strict_json(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "metadata", "components"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["components"], dict)
        or set(value["components"]) != set(COMPONENTS)
    ):
        raise ValueError("invalid publication snapshot schema")
    metadata = value["metadata"]
    if (
        not isinstance(metadata, dict)
        or set(metadata)
        != {"publication_inputs", "publication_inputs_digest", "profile", "accepted_empty"}
        or not isinstance(metadata["publication_inputs"], dict)
        or type(metadata["accepted_empty"]) is not bool
    ):
        raise ValueError("invalid publication snapshot metadata")
    try:
        components = {
            key + "_bytes": base64.b64decode(value["components"][key], validate=True)
            for key in COMPONENTS
        }
        result = PublicationInputs(
            build_year=metadata["publication_inputs"]["build_year"],
            **components,
            profile=metadata["profile"],
            accepted_empty_digest=metadata["publication_inputs_digest"]
            if metadata["accepted_empty"]
            else None,
        )
    except (KeyError, TypeError, binascii.Error) as exc:
        raise ValueError("invalid publication snapshot components") from exc
    if result.metadata() != metadata:
        raise ValueError("publication snapshot identity mismatch")
    return result


def load_publication_inputs(
    args: argparse.Namespace,
    *,
    legacy: Callable[[int, NamePolicy | None], PublicationInputs] | None = None,
) -> PublicationInputs:
    input_options(args)
    fd = getattr(args, "publication_inputs_fd", None)
    if fd is not None:
        if args.inputs is not None or any(getattr(args, key) is not None for key in COMPONENTS):
            raise ValueError("sealed snapshot conflicts with component paths or inputs document")
        result = snapshot_inputs(fd)
        if args.build_year is not None and args.build_year != result.build_year:
            raise ValueError("conflicting snapshot build year")
        if args.validation_profile is not None and args.validation_profile != result.profile:
            raise ValueError("conflicting snapshot validation profile")
        if (
            args.accepted_empty_inputs_digest is not None
            and args.accepted_empty_inputs_digest != result.accepted_empty_digest
        ):
            raise ValueError("conflicting snapshot empty-input allowance")
        args.build_year = result.build_year
    else:
        if args.build_year is None:
            raise ValueError("build year is required")
        paths = [getattr(args, key) for key in COMPONENTS]
        # A legacy name-policy alone remains supported in the private adapter.
        explicit = args.inputs is not None or any(p is not None for p in paths[1:])
        if explicit:
            if not all(p is not None for p in paths):
                raise ValueError("all four publication input files are required together")
            result = PublicationInputs.from_files(
                build_year=args.build_year,
                **dict(zip(COMPONENTS, paths, strict=True)),
                profile=args.validation_profile or "private-metadata",
                accepted_empty_digest=args.accepted_empty_inputs_digest,
            )
        else:
            if legacy is None:
                raise ValueError(
                    "explicit publication input files are required outside the private checkout"
                )
            if args.accepted_empty_inputs_digest is not None:
                raise ValueError("accepted empty input digest requires explicit component paths")
            result = legacy(
                args.build_year, NamePolicy.load(args.name_policy) if args.name_policy else None
            )
            if args.validation_profile is not None and args.validation_profile != result.profile:
                from dataclasses import replace

                result = replace(result, profile=args.validation_profile)
    if (
        args.publication_inputs_digest is not None
        and args.publication_inputs_digest != result.digest
    ):
        raise ValueError("publication input digest mismatch")
    return result


def validate_corpus(
    corpus: Path, inputs: PublicationInputs, asset_root: Path | None = None
) -> tuple[Any, list[dict[str, Any]]]:
    from .validation import CorpusValidator

    if not safe_path(corpus).is_dir():
        raise ValueError("corpus input is missing or not a directory")
    require_bytes = inputs.profile == "private-bytes"
    if require_bytes and (asset_root is None or not safe_path(asset_root).is_dir()):
        raise ValueError("private-bytes validation requires an explicit asset directory")
    validator = CorpusValidator()
    records = validator.validate(corpus, asset_root, require_bytes=require_bytes)
    if not records and inputs.profile != "synthetic":
        raise ValueError("private corpus input must not be empty")
    return validator, records


def write_metadata(path: Path | None, inputs: PublicationInputs, *, bytes_verified: bool) -> None:
    if path is not None:
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "verification": "bytes" if bytes_verified else "metadata",
                    **inputs.metadata(),
                },
                sort_keys=True,
            )
            + "\n"
        )
