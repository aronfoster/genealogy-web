"""Private, operator-controlled preauthorization; no Firebase calls or fake UIDs."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from .identity import normalize_email


def read_roster(path: Path, project: str) -> list[dict[str, str]]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,127}", project):
        raise ValueError("Invalid Firebase project")
    # Keep inputs out of any checkout, including a different working directory.
    resolved = path.resolve()
    if any((parent / ".git").exists() for parent in resolved.parents):
        raise ValueError("Keep the private roster outside source control")
    if any(parent.is_symlink() for parent in (path.absolute(), *path.absolute().parents)):
        raise ValueError("Roster path must not contain symlinks")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8-sig", newline="") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > 1024 * 1024
        ):
            raise ValueError("Roster must be a private, owner-only regular file")
        reader = csv.DictReader(io.StringIO(stream.read()))
    if reader.fieldnames != ["email", "person_id"]:
        raise ValueError("Expected email,person_id CSV header")
    entries = []
    seen = set()
    for row in reader:
        if set(row) != {"email", "person_id"} or any(v is None for v in row.values()):
            raise ValueError("Invalid roster row")
        _, email = normalize_email(row["email"])
        person = row["person_id"]
        if not re.fullmatch(r"per_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", person):
            raise ValueError("Invalid person ID")
        if email in seen:
            raise ValueError("Duplicate normalized email")
        seen.add(email)
        entries.append({"email": email, "project": project, "person_id": person})
    if not 1 <= len(entries) <= 1000:
        raise ValueError("Roster must contain 1 to 1000 entries")
    return entries


def enroll(connection: Any, args: Any, reader: Any) -> dict[str, Any]:
    entries = read_roster(args.input, args.project)
    # The importer takes the exclusive form before changing the active publication.
    connection.execute("SELECT pg_advisory_xact_lock_shared(105,2)")
    if not 1 <= len(args.actor.strip()) <= 200 or any(
        ord(c) < 32 or ord(c) == 127 for c in args.actor
    ):
        raise ValueError("Invalid operator actor")
    # The publication lock stabilizes these reads. Do not block sign-ins and
    # authorization decisions while connecting to a slow independent reader.
    target_query = "SELECT json_build_object('database',current_database(),'started',pg_postmaster_start_time()) AS target"
    database_target = connection.execute(target_query).fetchone()["target"]
    with reader.open("family") as repository:
        if repository.connection.execute(target_query).fetchone()[0] != database_target:
            raise ValueError("Operator and family reader must target the same database")
        publication = repository.listing_token()
        published = {
            entry["person_id"]: repository.get(entry["person_id"], ["people"]) is not None
            for entry in entries
        }
    # Lock before reading state; held until commit (or preview rollback). Existing
    # register, transition, rebind and link-person operations use this same lock.
    state = connection.execute(
        "SELECT authz.preauthorization_snapshot(%s) AS state", (Jsonb(entries),)
    ).fetchone()["state"]
    by_email = {row["email"]: row for row in state}
    rows = []
    for number, entry in enumerate(entries, 1):
        current = by_email[entry["email"]]
        pre, member, identity = (current[k] for k in ("preauthorization", "member", "identity"))
        if pre is None:
            action = "stage" if identity is None and not current["request_seen"] else "conflict"
        else:
            action = "unchanged"
            if (
                pre["project"] != entry["project"]
                or pre["person_id"] != entry["person_id"]
                or member["person_id"] != entry["person_id"]
                or member["state"] != "approved"
                or not member["reconciled"]
                or (identity is not None and identity["member_id"] != pre["member_id"])
            ):
                action = "conflict"
        review_row = {"row": number, "action": action}
        if member is not None:
            review_row.update(
                member_id=member["id"], version=member["version"], state=member["state"]
            )
        if not published[entry["person_id"]]:
            review_row["action"] = "unpublished-person"
        rows.append(review_row)
    # Include the connection target without exposing the DSN or hashing its password.
    target = {
        key: connection.info.get_parameters().get(key) for key in ("host", "port", "dbname", "user")
    }
    payload = {
        "entries": entries,
        "state": state,
        "publication": publication,
        "target": target,
        "actor": args.actor,
        "database": database_target,
    }
    review = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    result = {"rows": rows, "review": review, "committed": False, "staged": 0}
    if args.commit:
        if (
            not args.confirm
            or not re.fullmatch(r"[0-9a-f]{64}", args.confirm)
            or not hmac.compare_digest(args.confirm, review)
        ):
            raise ValueError("Review changed; preview again")
        if any(row["action"] not in {"stage", "unchanged"} for row in rows):
            raise ValueError("Batch contains conflicts")
        result["staged"] = connection.execute(
            "SELECT authz.preauthorize_batch(%s,%s,%s) AS staged",
            (Jsonb(entries), Jsonb(state), args.actor),
        ).fetchone()["staged"]
        result["committed"] = True
    elif args.confirm:
        raise ValueError("Confirmation requires --commit")
    return result
