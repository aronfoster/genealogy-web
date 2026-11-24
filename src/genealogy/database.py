"""Operator-run transactional SQL migrations with immutable checksum tracking."""

from __future__ import annotations

import hashlib
from importlib.resources import files
from typing import Any


def migrate_database(connection: Any) -> list[str]:
    applied: list[str] = []
    with connection.transaction():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(105, 1)")
            cur.execute("CREATE SCHEMA IF NOT EXISTS operations")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS operations.schema_migration (version text PRIMARY KEY, sha256 text NOT NULL)"
            )
            for resource in sorted(
                files("genealogy").joinpath("sql").iterdir(), key=lambda p: p.name
            ):
                if not resource.name.endswith(".sql"):
                    continue
                sql = resource.read_text()
                digest = hashlib.sha256(sql.encode()).hexdigest()
                cur.execute(
                    "SELECT sha256 FROM operations.schema_migration WHERE version=%s",
                    (resource.name,),
                )
                existing = cur.fetchone()
                if existing:
                    if existing[0] != digest:
                        raise ValueError(f"migration checksum changed: {resource.name}")
                    continue
                # Initial legacy script supplied its own transaction wrappers.
                sql = sql.removeprefix("BEGIN;\n").removesuffix("COMMIT;\n")
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO operations.schema_migration VALUES (%s,%s)",
                    (resource.name, digest),
                )
                applied.append(resource.name)
    return applied
