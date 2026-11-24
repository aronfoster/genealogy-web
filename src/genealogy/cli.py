from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path

from .database import migrate_database
from .ids import allocate
from .importer import import_corpus
from .input_options import load_publication_inputs, publication_arguments, write_metadata
from .migrations import migrate_corpus
from .model import canonical_bytes
from .name_policy import NamePolicy
from .publication import compile_publication
from .publication_inputs import PublicationInputs
from .validation import CorpusInvalid, CorpusValidator


def resolve_publication_inputs(args: argparse.Namespace) -> PublicationInputs:
    # Compatibility is anchored to accepted source, never corpus paths or a data head.
    package = Path(__file__).resolve().parent
    adapter = package.parents[1] / "scripts/publication_legacy.py"
    legacy = None
    if package.parent.name == "src" and adapter.is_file():
        spec = importlib.util.spec_from_file_location(
            "_genealogy_private_publication_adapter", adapter
        )
        if spec is None or spec.loader is None:
            raise ValueError("private publication adapter unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        legacy = module.legacy_inputs
    return load_publication_inputs(args, legacy=legacy)


def main(*, legacy: Callable[[int, NamePolicy | None], PublicationInputs] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="genealogy")
    sub = parser.add_subparsers(dest="command", required=True)
    ids = sub.add_parser("allocate")
    ids.add_argument("type")
    ids.add_argument("count", nargs="?", type=int, default=1)
    validate = sub.add_parser("validate")
    validate.add_argument("corpus", type=Path)
    validate.add_argument("--asset-root", type=Path)
    migrate = sub.add_parser("migrate")
    migrate.add_argument("corpus", type=Path)
    migrate.add_argument("--from-version", required=True, type=int)
    compile_cmd = sub.add_parser("compile")
    compile_cmd.add_argument("corpus", type=Path)
    compile_cmd.add_argument("output", type=Path)
    compile_cmd.add_argument("--audience", choices=("public", "family"), required=True)
    dbm = sub.add_parser("db-migrate")
    dbm.add_argument("database_url")
    compile_cmd.add_argument("--build-year", type=int)
    compile_cmd.add_argument("--asset-root", type=Path)
    compile_cmd.add_argument("--name-policy", type=Path)
    imp = sub.add_parser("import")
    imp.add_argument("corpus", type=Path)
    imp.add_argument("database_url")
    imp.add_argument("--commit", required=True)
    imp.add_argument("--production", action="store_true")
    imp.add_argument("--accepted-branch", default="origin/main")
    imp.add_argument("--asset-root", type=Path)
    imp.add_argument("--build-year", type=int)
    imp.add_argument("--name-policy", type=Path)
    publication_arguments(compile_cmd)
    publication_arguments(imp)
    args = parser.parse_args()
    try:
        if args.command == "allocate":
            print("\n".join(allocate(args.type, args.count)))
        elif args.command == "validate":
            validator = CorpusValidator()
            records = validator.validate(args.corpus, args.asset_root)
            for warning in validator.warnings:
                print(f"warning: {warning}", file=sys.stderr)
            print(
                f"valid: {len(records)} records; {len(validator.unverified_assets)} assets unverified"
            )
        elif args.command == "migrate":
            migrate_corpus(args.corpus, args.from_version)
            CorpusValidator().validate(args.corpus)
        elif args.command == "compile":
            inputs = (
                load_publication_inputs(args, legacy=legacy)
                if legacy
                else resolve_publication_inputs(args)
            )
            if args.asset_root is None:
                raise ValueError("asset root is required")
            records = CorpusValidator().validate(args.corpus, args.asset_root, require_bytes=True)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(
                canonical_bytes(
                    compile_publication(
                        records,
                        args.audience,
                        inputs=inputs,
                    )
                )
            )
        elif args.command == "db-migrate":
            import psycopg

            with psycopg.connect(args.database_url) as connection:
                print(json.dumps(migrate_database(connection)))
        elif args.command == "import":
            inputs = (
                load_publication_inputs(args, legacy=legacy)
                if legacy
                else resolve_publication_inputs(args)
            )
            if args.asset_root is None:
                raise ValueError("asset root is required")
            import psycopg

            with psycopg.connect(args.database_url) as connection:
                print(
                    json.dumps(
                        import_corpus(
                            args.corpus,
                            connection,
                            args.commit,
                            production=args.production,
                            accepted_branch=args.accepted_branch,
                            asset_root=args.asset_root,
                            build_year=args.build_year,
                            publication_inputs=inputs,
                            name_policy=inputs.name_policy,
                        ),
                        sort_keys=True,
                    )
                )
        if args.command in {"compile", "import"} and args.input_metadata:
            write_metadata(args.input_metadata, inputs, bytes_verified=True)
    except (CorpusInvalid, ValueError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
