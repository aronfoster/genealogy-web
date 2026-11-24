"""Operator-only decisions, with an independent family reader for person checks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from genealogy.auth.identity import normalize_email
from genealogy.web.repository import PostgresStore

BASE_TABLES = (
    "member",
    "email_identity",
    "provider_binding",
    "family_request",
    "decision",
    "membership",
    "access_request",
)
TABLES = (*BASE_TABLES, "preauthorization")
OWNER_TABLES = (*TABLES, "owner_binding", "owner_audit", "owner_operation")
FEEDBACK_TABLES = (*OWNER_TABLES, "feedback_receipt", "feedback_restore_guard")
REVIEWED_INVENTORIES = (
    frozenset(BASE_TABLES),
    frozenset(TABLES),
    frozenset(OWNER_TABLES),
    frozenset(FEEDBACK_TABLES),
)
"""Append reviewed historical inventories; never replace or remove deployed sets.

Schema integrations must retain every supported archive shape and extend the
restore order to cover their union. Unknown or incomplete inventories fail closed.
"""


def authz_tables(connection: Any) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='authz' AND c.relkind IN ('r','p')"
    ).fetchall()
    names = {row["relname"] for row in rows}
    if frozenset(names) in REVIEWED_INVENTORIES:
        return tuple(table for table in FEEDBACK_TABLES if table in names)
    raise ValueError("Unreviewed authorization table inventory")


LEGACY_TABLES = TABLES[:7]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="genealogy-access")
    commands = result.add_subparsers(dest="command", required=True)
    for name in (
        "members",
        "identities",
        "link-report",
        "requests",
        "history",
        "legacy",
        "purge-prose",
        "owners",
    ):
        commands.add_parser(name)
    add = commands.add_parser("owner-add")
    add.add_argument("--project", required=True)
    add.add_argument("--uid", required=True)
    add.add_argument("--email", required=True)
    add.add_argument("--actor", required=True)
    add.add_argument("--note", default="")
    retire = commands.add_parser("owner-retire")
    retire.add_argument("owner", type=UUID)
    retire.add_argument("--actor", required=True)
    retire.add_argument("--note", default="")
    enrollment = commands.add_parser("preauthorize")
    enrollment.add_argument("input", type=Path)
    enrollment.add_argument("--project", required=True)
    enrollment.add_argument("--actor", required=True)
    enrollment.add_argument("--commit", action="store_true")
    enrollment.add_argument("--confirm")
    export = commands.add_parser("export")
    export.add_argument("output", type=Path)
    quarantine = commands.add_parser("quarantine-restore")
    quarantine.add_argument("--actor", required=True)
    for name in (
        "approve",
        "revoke",
        "reapprove",
        "reconcile",
        "rebind",
        "dismiss-request",
        "link-person",
    ):
        cmd = commands.add_parser(name)
        cmd.add_argument("member", type=UUID)
        cmd.add_argument("--version", type=int, required=True)
        cmd.add_argument("--actor", required=True)
        cmd.add_argument("--note", default="")
        if name in {"rebind", "dismiss-request"}:
            cmd.add_argument("--request", type=UUID, required=True)
        if name == "link-person":
            cmd.add_argument("--person")
    return result


def run(connection: Any, args: argparse.Namespace) -> Any:
    command = args.command
    if command == "owners":
        return connection.execute(
            "SELECT * FROM authz.owner_binding ORDER BY created_at,id"
        ).fetchall()
    if command == "preauthorize":
        from .enrollment import enroll

        return enroll(connection, args, family_reader())
    if command == "members":
        return connection.execute("SELECT * FROM authz.member ORDER BY created_at,id").fetchall()
    if command == "identities":
        return connection.execute(
            """SELECT m.id AS member_id,m.version,m.state,m.reconciled,m.person_id,
            e.original_email,b.project,b.uid,b.active
            FROM authz.member m JOIN authz.email_identity e ON e.member_id=m.id
            LEFT JOIN authz.provider_binding b ON b.identity_id=e.id
            ORDER BY e.normalized_email,b.created_at"""
        ).fetchall()
    if command == "link-report":
        rows = connection.execute(
            "SELECT id AS member_id,version,person_id FROM authz.member WHERE person_id IS NOT NULL ORDER BY id"
        ).fetchall()
        with family_reader().open("family") as repository:
            return [
                {**row, "available": repository.get(row["person_id"], ["people"]) is not None}
                for row in rows
            ]
    if command == "requests":
        return connection.execute(
            "SELECT * FROM authz.family_request WHERE decided_at IS NULL ORDER BY created_at,id"
        ).fetchall()
    if command == "history":
        return connection.execute("SELECT * FROM authz.decision ORDER BY occurred_at,id").fetchall()
    if command == "legacy":
        return {
            "members": connection.execute("SELECT * FROM authz.membership").fetchall(),
            "requests": connection.execute("SELECT * FROM authz.access_request").fetchall(),
        }
    if command == "purge-prose":
        return connection.execute("SELECT authz.purge_prose() AS purged").fetchone()
    if command == "quarantine-restore":
        return connection.execute(
            "SELECT authz.quarantine_restore(%s) AS quarantined", (args.actor,)
        ).fetchone()
    if command == "export":
        # A consistent review/export artifact, NOT an executable restore script.
        payload: dict[str, Any] = {"format": "genealogy-authz-v1", "tables": {}}
        for table in authz_tables(connection):
            rows = connection.execute(
                psycopg.sql.SQL("SELECT * FROM authz.{}").format(psycopg.sql.Identifier(table))
            ).fetchall()
            payload["tables"][table] = rows
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(payload, output, default=str, indent=2)
            output.write("\n")
        return {"exported": True}
    if len(args.note) > 500 or any(ord(c) < 32 for c in args.note):
        raise ValueError("Use a brief single-line operator note (at most 500 characters)")
    if command == "owner-add":
        return connection.execute(
            "SELECT authz.owner_add(%s,%s,%s,%s,%s) AS owner_id",
            (args.project, args.uid, normalize_email(args.email)[1], args.actor, args.note),
        ).fetchone()
    if command == "owner-retire":
        return connection.execute(
            "SELECT authz.owner_retire(%s,%s,%s) AS retired", (args.owner, args.actor, args.note)
        ).fetchone()
    if command == "rebind":
        return connection.execute(
            "SELECT authz.rebind(%s,%s,%s,%s,%s) AS version",
            (args.member, args.version, args.request, args.actor, args.note),
        ).fetchone()
    if command == "dismiss-request":
        return connection.execute(
            "SELECT authz.dismiss_request(%s,%s,%s,%s,%s) AS version",
            (args.member, args.version, args.request, args.actor, args.note),
        ).fetchone()
    if command == "link-person":
        if args.person is not None:
            with family_reader().open("family") as repository:
                if repository.get(args.person, ["people"]) is None:
                    raise ValueError("Person is not in the family publication")
        return connection.execute(
            "SELECT authz.link_person(%s,%s,%s,%s,%s) AS version",
            (args.member, args.version, args.person, args.actor, args.note),
        ).fetchone()
    return connection.execute(
        "SELECT authz.transition(%s,%s,%s,%s,%s) AS version",
        (args.member, args.version, command, args.actor, args.note),
    ).fetchone()


def family_reader() -> PostgresStore:
    """Independent reader credentials; the operator gains no corpus privileges."""
    dsn = os.environ.get("GENEALOGY_FAMILY_DSN")
    if not dsn:
        raise ValueError("Set the separate GENEALOGY_FAMILY_DSN for person checks")
    return PostgresStore("", dsn)


def main() -> None:
    cli = parser()
    args = cli.parse_args()
    dsn = os.environ.get("GENEALOGY_OPERATOR_DSN")
    if not dsn:
        cli.error("Set GENEALOGY_OPERATOR_DSN to the separate operator connection")
    try:
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
            if args.command == "export":
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SET LOCAL statement_timeout='10s'")
            connection.execute("SET LOCAL ROLE genealogy_authz_operator")
            output = run(connection, args)
        print(json.dumps(output, default=str, indent=2))
    except (psycopg.Error, ValueError, OSError, RuntimeError):
        # DB errors may embed personal data/secrets. A lost COMMIT response (or
        # failed stdout after commit) cannot establish that the transaction failed.
        cli.exit(
            2,
            "Operation failed or its commit could not be confirmed. Check operator/reader permissions, current member/version, published person, and command arguments. Review current state before retrying; a commit may already have completed.\n",
        )


if __name__ == "__main__":
    main()
