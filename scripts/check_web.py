"""Disposable native-PostgreSQL application browser check; never a production server."""

from __future__ import annotations

import argparse
import os
import socket
import tempfile
import threading
import uuid
from pathlib import Path

import psycopg
import uvicorn
from browser_inputs import ScenarioInputs, run_browser
from psycopg import sql
from psycopg.conninfo import make_conninfo
from serving_statistics import STATISTICS_SQL
from starlette.responses import JSONResponse
from starlette.routing import Route

from genealogy.database import migrate_database
from genealogy.importer import import_corpus
from genealogy.input_options import (
    input_options,
    load_publication_inputs,
    publication_arguments,
    safe_path,
    validate_corpus,
    write_metadata,
)
from genealogy.model import write_record
from genealogy.publication import compile_publications
from genealogy.publication_inputs import read_input, strict_json
from genealogy.web.app import create_app
from genealogy.web.repository import PostgresStore


def serialized_migration(
    cluster: psycopg.Connection[tuple[object, ...]],
    admin: psycopg.Connection[tuple[object, ...]],
) -> None:
    # Databases are isolated, but migration roles are cluster objects. Every
    # browser harness must take this lock in the same coordinator database;
    # advisory locks in each worker's private database cannot serialize them.
    with cluster.transaction():
        cluster.execute("SELECT pg_advisory_xact_lock(hashtext('genealogy-browser-schema'))")
        migrate_database(admin)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical", action="store_true", help="Reproduce the frozen workshop")
    parser.add_argument(
        "--family",
        action="store_true",
        help="Check the approved-family projection in the disposable harness",
    )
    parser.add_argument(
        "--source-preview",
        action="store_true",
        help="Overlay workshop source inputs in a disposable preview",
    )
    parser.add_argument("--names-preview", action="store_true", help="Overlay workshop name inputs")
    publication_arguments(parser)
    parser.add_argument("--expectations", type=Path)
    parser.add_argument("--historical-root", type=Path)
    parser.add_argument("--source-overlay", type=Path)
    parser.add_argument("--names-overlay", type=Path)
    args = parser.parse_args()
    input_options(args)
    if args.historical and (args.source_preview or args.names_preview):
        parser.error("preview mode requires the canonical corpus")
    repo = Path(__file__).resolve().parents[1]
    if args.historical_root and not args.historical:
        parser.error("--historical-root requires --historical")
    if (
        args.source_overlay
        and not args.source_preview
        or args.names_overlay
        and not args.names_preview
    ):
        parser.error("overlay paths require the corresponding preview")
    kind = "historical" if args.historical else "current"
    if not args.expectations or not args.inputs:
        parser.error("workshop expectations and explicit inputs are required")
    if args.historical and not args.historical_root:
        parser.error("historical workshop requires an explicit historical root")
    if (
        args.source_preview
        and not args.source_overlay
        or args.names_preview
        and not args.names_overlay
    ):
        parser.error("preview requires its explicit workshop overlay")
    scenario = ScenarioInputs.load(args.expectations, kind)
    if args.historical:
        root = safe_path(args.historical_root)
        if args.corpus and args.corpus != root / "corpus":
            parser.error("historical corpus conflicts with historical root")
        args.corpus = root / "corpus"
        record_map = strict_json(read_input(root / "record-map.json"))
        expected = strict_json(scenario.raw)
        if any(expected[key] != record_map[key] for key in ("senior", "child", "infant")):
            raise ValueError("historical expectations differ from frozen record map")
    if args.corpus is None:
        parser.error("explicit workshop corpus is required")
    inputs = load_publication_inputs(args)
    if inputs.profile != "synthetic":
        parser.error("workshop requires the synthetic profile")
    policy = inputs.name_policy
    corpus = safe_path(args.corpus)
    overlays = []
    if args.source_preview:
        overlays.append(args.source_overlay)
    if args.names_preview:
        overlays.append(args.names_overlay)
    # Stage inert files by their confined relative paths, never paths computed
    # from a record's unvalidated type or ID. Validate the complete merged set.
    with tempfile.TemporaryDirectory() as staging:
        target = Path(staging)
        for source in [corpus, *overlays]:
            source = safe_path(source)
            paths = list(source.rglob("*"))
            if not any(p.suffix == ".json" for p in paths):
                raise ValueError("browser corpus or overlay is empty")
            for path in paths:
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    raise ValueError("browser corpus and overlays require regular input paths")
                if path.is_file() and path.suffix == ".json":
                    destination = target / path.relative_to(source)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(read_input(path))
        _, records = validate_corpus(target, inputs, args.asset_root)
    public, family, _ = compile_publications(records, inputs=inputs)
    policy.assert_all_used(public, family)
    visible = [p["id"] for p in (family if args.family else public)["people"]]
    if not visible:
        raise ValueError("browser corpus needs at least one visible person")
    url = os.environ["TEST_DATABASE_URL"]
    name = "genealogy_browser_" + uuid.uuid4().hex
    role = name + "_reader"
    family_role = name + "_family_reader"
    with psycopg.connect(url, autocommit=True) as cluster:
        cluster.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            dburl = make_conninfo(url, dbname=name)
            with (
                psycopg.connect(dburl, autocommit=True) as admin,
                tempfile.TemporaryDirectory() as directory,
            ):
                serialized_migration(cluster, admin)
                admin.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'browser-test-only'").format(
                        sql.Identifier(role)
                    )
                )
                admin.execute(
                    sql.SQL("GRANT genealogy_public_ro TO {}").format(sql.Identifier(role))
                )
                admin.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'browser-test-only'").format(
                        sql.Identifier(family_role)
                    )
                )
                admin.execute(
                    sql.SQL("GRANT genealogy_family_ro TO {}").format(sql.Identifier(family_role))
                )
                # Real claims, no unpublished PDF bytes in CI. Rights handling is
                # independently exercised with byte-verified synthetic fixtures.
                for record in records:
                    if record["type"] != "asset":
                        write_record(Path(directory), record)
                import_corpus(
                    Path(directory),
                    admin,
                    "0" * 40,
                    build_year=inputs.build_year,
                    publication_inputs=inputs,
                    name_policy=policy,
                )
                # Match deployment readiness; fresh imports must not race
                # autovacuum for the planner statistics used by reader queries.
                admin.execute(STATISTICS_SQL)
            store = PostgresStore(
                make_conninfo(dburl, user=role, password="browser-test-only"),
                make_conninfo(dburl, user=family_role, password="browser-test-only"),
            )
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                base = "http://127.0.0.1:" + str(listener.getsockname()[1])
                app = create_app(
                    store=store, authorize_family=(lambda _: True) if args.family else None
                )
                if args.family:
                    # This disposable reader harness has an approved audience, not
                    # a real session. Auth lifecycle is checked by check_auth_browser.py.
                    app.router.routes.append(
                        Route("/api/access/status", lambda _: JSONResponse({"allowed": True}))
                    )
                config = uvicorn.Config(app, access_log=False, log_level="warning")
                server = uvicorn.Server(config)
                worker = threading.Thread(
                    target=server.run, kwargs={"sockets": [listener]}, daemon=True
                )
                worker.start()
                try:
                    run_browser(
                        repo / "scripts/check_web.mjs",
                        scenario.raw,
                        {
                            "WEB_TEST_BASE_URL": base,
                            "WEB_TEST_PEOPLE": ",".join(visible[:3]),
                            "WEB_TEST_SOURCE_COUNT": str(
                                len((family if args.family else public)["sources"])
                            ),
                            "WEB_TEST_NEWSPAPER_COUNT": str(
                                sum(
                                    source["source_kind"] == "newspaper"
                                    for source in (family if args.family else public)["sources"]
                                )
                            ),
                            "WEB_TEST_HISTORICAL": "1" if args.historical else "0",
                            "WEB_TEST_FAMILY": "1" if args.family else "0",
                            "WEB_TEST_SOURCE_PREVIEW": "1" if args.source_preview else "0",
                            "WEB_TEST_NAMES_PREVIEW": "1" if args.names_preview else "0",
                        },
                        scenario,
                    )
                finally:
                    server.should_exit = True
                    worker.join(timeout=10)
        finally:
            cluster.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            cluster.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
            cluster.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(family_role)))

    write_metadata(args.input_metadata, inputs, bytes_verified=inputs.profile == "private-bytes")


if __name__ == "__main__":
    main()
