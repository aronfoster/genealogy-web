from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .model import canonical_bytes, read_records
from .name_policy import NamePolicy
from .projection_contract import BASELINE, COLLECTIONS, FEATURES, descriptor, verify_descriptor
from .publication import compile_publications
from .publication_inputs import PublicationInputs
from .validation import CorpusValidator


def verify_production_checkout(
    root: Path, commit: str, accepted_branch: str, corpus: Path | None = None
) -> None:
    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, check=True
            ).stdout.strip()
        except (subprocess.CalledProcessError, OSError) as exc:
            # Do not echo stderr/URLs: a remote may contain credentials.
            raise ValueError(
                f"production Git {args[0]} failed; check repository/remote access and retry (no import performed)"
            ) from exc

    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("production import refuses a dirty worktree")
    if git("rev-parse", "HEAD") != commit:
        raise ValueError("pinned commit is not checked out")
    # Never rely on a stale remote tracking ref; a failed fetch blocks production.
    if not accepted_branch.startswith("origin/"):
        raise ValueError("accepted branch must be an origin remote branch")
    branch = accepted_branch.removeprefix("origin/")
    git("fetch", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    if (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, accepted_branch],
            cwd=root,
            capture_output=True,
        ).returncode
        != 0
    ):
        raise ValueError(f"commit is not reachable from {accepted_branch}")
    if corpus:
        relative = corpus.resolve().relative_to(root.resolve()).as_posix()
        tracked = set(git("ls-tree", "-r", "--name-only", commit, "--", relative).splitlines())
        actual = {p.relative_to(root).as_posix() for p, _ in read_records(corpus)}
        if actual != {p for p in tracked if p.endswith(".json")}:
            raise ValueError("corpus files do not match pinned Git tree")
        for path in actual:
            content = subprocess.run(
                ["git", "show", f"{commit}:{path}"], cwd=root, capture_output=True, check=True
            ).stdout
            if content != (root / path).read_bytes():
                raise ValueError(f"corpus differs from pinned Git tree: {path}")


def deterministic_rows(records: list[dict[str, Any]]) -> list[tuple[str, str, str, str, bool]]:
    return [
        (r["id"], r["type"], r["visibility"], canonical_bytes(r).decode().strip(), r["retired"])
        for r in sorted(records, key=lambda x: x["id"])
        if r["type"] not in {"consent", "research_question"}
    ]


def assertion_subjects(record: dict[str, Any]) -> list[tuple[str, str]]:
    typ, value = record["assertion_type"], record["value"]
    if typ == "relationship.parent_child":
        return [("child", value["child_id"]), ("parent", value["parent_id"])]
    result = [("subject", record["subject_id"])]
    if "other_id" in value:
        result.append(("other", value["other_id"]))
    return result


def import_corpus(
    corpus: Path,
    connection: Any,
    commit: str,
    *,
    production: bool = False,
    repo: Path | None = None,
    accepted_branch: str = "origin/main",
    asset_root: Path | None = None,
    build_year: int,
    name_policy: NamePolicy | None = None,
    publication_inputs: PublicationInputs,
    profile: str = BASELINE,
    descriptor_bytes: bytes | None = None,
    descriptor_sha256: str = "",
    target_runtime: str = "",
) -> dict[str, int]:
    if publication_inputs.build_year != build_year or publication_inputs.name_policy != (
        name_policy or NamePolicy()
    ):
        raise ValueError("conflicting importer publication inputs")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise ValueError("commit must be a full lowercase SHA-1 or SHA-256 object ID")
    if production:
        verify_production_checkout(
            (repo or corpus.parent).resolve(), commit, accepted_branch, corpus.resolve()
        )
    validator = CorpusValidator()
    records = validator.validate(corpus, asset_root, require_bytes=True)
    public, family, effective = compile_publications(
        records,
        inputs=publication_inputs,
        profile=profile,
        verified_portraits=frozenset(validator.verified_portraits),
    )
    if (
        descriptor_bytes is not None
        or descriptor_sha256
        or target_runtime
        or profile != BASELINE
        or any(
            r.get("assertion_type") in FEATURES or "portrait" in r or r.get("profile_only")
            for r in records
        )
    ):
        if descriptor_bytes is None:
            raise ValueError("split import requires an accepted projection descriptor")
        expected = descriptor(
            public,
            family,
            profile,
            target_runtime,
            publication_inputs.metadata()["publication_inputs_digest"],
        )
        verify_descriptor(descriptor_bytes, descriptor_sha256, expected)
    generation = hashlib.sha256(
        canonical_bytes(
            {
                "commit": commit,
                "records": sorted(records, key=lambda r: r["id"]),
                "public": public,
                "family": family,
                "build_year": build_year,
                "name_policy": (name_policy or NamePolicy()).document(),
            }
        )
    ).hexdigest()
    rows = deterministic_rows(records)
    subjects = [
        (generation, r["id"], role, ref)
        for r in records
        if r["type"] == "assertion"
        for role, ref in assertion_subjects(r)
    ]
    evidence = [
        (generation, r["id"], e["citation_id"], e["stance"])
        for r in records
        if r["type"] == "assertion"
        for e in r["evidence"]
    ]
    serving = []
    serving_subjects = []
    for doc in (public, family):
        for category in COLLECTIONS:
            for item in doc[category]:
                serving.append(
                    (
                        generation,
                        doc["audience"],
                        item["id"],
                        category,
                        json.dumps(item, sort_keys=True),
                    )
                )
                if category in {"assertions", "alternatives", "history"}:
                    for role, ref in assertion_subjects(item):
                        serving_subjects.append(
                            (generation, doc["audience"], item["id"], role, ref)
                        )
    with connection.transaction():
        with connection.cursor() as cur:
            # Serialize concurrent writers; MVCC readers keep the old generation.
            cur.execute("SELECT pg_advisory_xact_lock(105, 2)")
            cur.execute("SELECT genealogy.prepare_generation(%s)", (generation,))
            cur.executemany(
                "INSERT INTO genealogy.entity_stage(id,entity_type,visibility,document,retired,generation) VALUES (%s,%s,%s,%s::jsonb,%s,%s)",
                [row + (generation,) for row in rows],
            )
            cur.executemany(
                "INSERT INTO genealogy.assertion_subject VALUES (%s,%s,%s,%s)", subjects
            )
            cur.executemany(
                "INSERT INTO genealogy.assertion_evidence VALUES (%s,%s,%s,%s)", evidence
            )
            cur.executemany(
                "INSERT INTO genealogy.publication_decision VALUES (%s,%s,%s)",
                [(generation, row[0], effective[row[0]]) for row in rows],
            )
            cur.executemany(
                "INSERT INTO genealogy.serving_entity VALUES (%s,%s,%s,%s,%s::jsonb)", serving
            )
            cur.executemany(
                "INSERT INTO genealogy.serving_subject VALUES (%s,%s,%s,%s,%s)", serving_subjects
            )
            cur.execute(
                "INSERT INTO genealogy.projection_stage(audience,document,generation) VALUES ('public',%s::jsonb,%s),('family',%s::jsonb,%s)",
                (
                    json.dumps(public, sort_keys=True),
                    generation,
                    json.dumps(family, sort_keys=True),
                    generation,
                ),
            )
            cur.execute(
                "SELECT genealogy.activate_generation(%s,%s,%s,%s)",
                (generation, commit, 1, len(rows)),
            )
    return {
        "authority": len(rows),
        "public_assertions": len(public["assertions"]),
        "family_assertions": len(family["assertions"]),
    }
