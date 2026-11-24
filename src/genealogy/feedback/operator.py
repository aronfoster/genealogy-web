"""Attended receipt inspection/reconciliation. Never creates YouTrack issues."""

from __future__ import annotations

import argparse
import json
import os
from uuid import UUID

import psycopg

from .config import FeedbackConfig
from .delivery import FeedbackDelivery
from .ledger import Ledger
from .youtrack import YouTrackClient


def main() -> None:
    cli = argparse.ArgumentParser(prog="genealogy-feedback")
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("pending")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("key", type=UUID)
    reconcile.add_argument(
        "--issue", help="Verified GEN number for direct read when search is stale"
    )
    args = cli.parse_args()
    try:
        if args.command == "pending":
            # Operator permission only. Output has references/times, never hashes
            # of identity/payload, report text, email or provider response bodies.
            with psycopg.connect(
                os.environ["GENEALOGY_OPERATOR_DSN"], connect_timeout=5
            ) as connection:
                connection.execute("SET LOCAL ROLE genealogy_authz_operator")
                connection.execute("SET LOCAL statement_timeout='5s'")
                rows = connection.execute(
                    "SELECT client_key,created_at FROM authz.feedback_receipt WHERE state='pending' ORDER BY created_at,client_key"
                ).fetchall()
                print(
                    json.dumps([{"key": str(r[0]), "created_at": r[1].isoformat()} for r in rows])
                )
        else:
            # Deliberately works with the web mode OFF; loading secrets into this
            # attended process never enables the visitor form.
            config = FeedbackConfig(
                os.environ["GENEALOGY_YOUTRACK_TOKEN"],
                os.environ["GENEALOGY_YOUTRACK_BASE_URL"],
                os.environ["GENEALOGY_YOUTRACK_PROJECT_ID"],
                os.environ["GENEALOGY_YOUTRACK_REVIEWED_PROJECT_ID"],
            )
            delivery = FeedbackDelivery(
                YouTrackClient(config), Ledger(os.environ["GENEALOGY_AUTHZ_DSN"])
            )
            issue = delivery.reconcile(str(args.key), args.issue)
            print(json.dumps({"issue": issue.identifier}))
    except Exception:
        # No argv/env/provider/SQL exceptions in terminal diagnostics.
        print("Feedback reconciliation unconfirmed; no issue was created.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
