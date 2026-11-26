import argparse
import copy
import hashlib
import json
import os
import struct
import subprocess
import sys
import uuid
import zlib

import psycopg
import pytest
from browser_scenarios.records import identifier
from check_synthetic_browser import materialize
from check_web import serialized_migration
from portrait_proxy import proxy_client
from psycopg import sql
from psycopg.conninfo import make_conninfo
from starlette.testclient import TestClient

from genealogy.importer import import_corpus
from genealogy.input_options import load_publication_inputs
from genealogy.model import canonical_bytes, write_record
from genealogy.portrait import safe_png
from genealogy.projection_contract import BASELINE, ENHANCED, descriptor, digest, validate_output
from genealogy.publication import compile_publications
from genealogy.validation import CorpusInvalid, CorpusValidator
from genealogy.web.app import create_app
from genealogy.web.repository import PostgresStore

BASELINE_SHA = "bff431ac4bba664f2e2b7250c995150fbca4d153"
TARGET = "2" * 40  # Synthetic accepted target for compatibility tests.


def png():
    def chunk(kind, value):
        return (
            struct.pack(">I", len(value))
            + kind
            + value
            + struct.pack(">I", zlib.crc32(kind + value))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\x00\x80\x80"))
        + chunk(b"IEND", b"")
    )


def samples(root, audience="family"):
    args = argparse.Namespace(inputs=materialize(root, historical=True))
    inputs = load_publication_inputs(args)
    corpus = root / "corpus"
    assets = root / "assets"
    assets.mkdir(exist_ok=True)

    def rid(kind, n):
        return identifier(kind, n, historical=True)

    def add(kind, n, **fields):
        row = dict(schema_version=1, type=kind, id=rid(kind, n), visibility=audience, retired=False)
        row.update(fields)
        write_record(corpus, row)
        return row

    person, second = rid("person", 1), rid("person", 2)
    source = add(
        "source",
        600,
        title="Painted workshop portrait",
        source_kind="record_image",
        redistribution="permitted",
        rights_basis="permission",
        profile_only=True,
    )
    citation = add(
        "citation",
        600,
        source_id=source["id"],
        locator="Invented portrait card",
        inspection="original_image_inspected",
        transcription="",
        transcription_visibility=audience,
        profile_only=True,
    )
    blob = png()
    checksum = hashlib.sha256(blob).hexdigest()
    (assets / checksum).write_bytes(blob)
    asset = add(
        "asset",
        600,
        source_id=source["id"],
        label="Studio derivative",
        redistribution="permitted",
        rights_basis="permission",
        media_type="image/png",
        storage_key=checksum,
        byte_size=len(blob),
        sha256=checksum,
        portrait={"depicted_people": [person, second], "attribution": "Workshop artist"},
    )
    for i, subject in enumerate((person, second)):
        add(
            "consent",
            600 + i,
            visibility="admin",
            decision="grant",
            audience=audience,
            recorded_at="2026-01-01",
            method="Invented workshop permission",
            person_id=subject,
            scope=[asset["id"]],
        )
    for i, (typ, value, cit) in enumerate(
        [
            ("person.favorite_color", {"text": "teal <blue>"}, rid("citation", 1)),
            ("person.photo", {"asset_id": asset["id"]}, citation["id"]),
        ]
    ):
        add(
            "assertion",
            600 + i,
            assertion_type=typ,
            subject_id=person,
            value=value,
            origin="researcher_conclusion",
            reasoning="Invented studio inventory.",
            status="accepted",
            dependencies=[rid("assertion", 1)],
            evidence=[{"citation_id": cit, "stance": "supports", "explicitness": "explicit"}],
        )
    return inputs, person, asset


def outputs(root, inputs, profile):
    validator = CorpusValidator()
    records = validator.validate(root / "corpus", root / "assets", require_bytes=True)
    return compile_publications(
        records,
        inputs=inputs,
        profile=profile,
        verified_portraits=frozenset(validator.verified_portraits),
    )[:2]


@pytest.mark.parametrize("audience", ["public", "family"])
def test_visible_profile_and_downgrade(tmp_path, audience):
    inputs, person, asset = samples(tmp_path, audience)
    for profile in (BASELINE, ENHANCED):
        public, family = outputs(tmp_path, inputs, profile)
        for doc in (public, family):
            validate_output(doc, doc["audience"], profile)
            row = next(r for r in doc["people"] if r["id"] == person)
            expected = profile == ENHANCED and (audience == "public" or doc is family)
            assert ("profile_photo" in row) == expected
            assert ("favorite_color" in row) == expected
            assert (asset["id"] in str(doc)) == expected
            assert ("Painted workshop portrait" in str(doc)) == expected
            assert identifier("source", 1, historical=True) in str(doc)
            assert not any(
                r.get("assertion_type") in ("person.photo", "person.favorite_color")
                for r in doc["assertions"]
            )
            if expected:
                assert row["profile_photo"] == {"asset_id": asset["id"]}
                assert row["favorite_color"] == "teal <blue>"


@pytest.mark.parametrize(
    ("kind", "n", "change"),
    [
        ("consent", 601, {"decision": "withdraw"}),
        ("consent", 601, {"decision": "expired"}),
        ("consent", 601, {"retired": True, "retirement_reason": "Removed grant"}),
        ("source", 600, {"rights_basis": "unknown"}),
        ("asset", 600, {"redistribution": "restricted"}),
        ("assertion", 601, {"status": "candidate"}),
    ],
)
def test_portrait_denial_closes_catalog(tmp_path, kind, n, change):
    inputs, person, asset = samples(tmp_path)
    path = tmp_path / "corpus" / kind / (identifier(kind, n, historical=True) + ".json")
    record = json.loads(path.read_text())
    record.update(change)
    write_record(tmp_path / "corpus", record)
    for doc in outputs(tmp_path, inputs, ENHANCED):
        assert asset["id"] not in str(doc) and asset["sha256"] not in str(doc)
        assert "profile_photo" not in str(doc)
        assert "Painted workshop portrait" not in str(doc)


def test_malformed_color_and_derivative(tmp_path):
    inputs, _, asset = samples(tmp_path)
    assert safe_png(png()) and not safe_png(png() + b"private metadata")
    path = next((tmp_path / "corpus" / "assertion").glob("*000000000258.json"))
    record = json.loads(path.read_text())
    record["value"]["text"] = " "
    write_record(tmp_path / "corpus", record)
    with pytest.raises(CorpusInvalid):
        outputs(tmp_path, inputs, ENHANCED)


def test_fail_before_transaction(tmp_path):
    inputs, _, _ = samples(tmp_path)
    public, family = outputs(tmp_path, inputs, ENHANCED)
    raw = canonical_bytes(
        descriptor(public, family, ENHANCED, TARGET, inputs.metadata()["publication_inputs_digest"])
    )
    args = dict(
        build_year=inputs.build_year,
        asset_root=tmp_path / "assets",
        publication_inputs=inputs,
        profile=ENHANCED,
        target_runtime=TARGET,
    )
    # A connection with no SQL methods detects premature database access.
    for changed in (
        {"descriptor_bytes": None},
        {"descriptor_bytes": raw + b" "},
        {"descriptor_bytes": raw, "target_runtime": "1" * 40},
        {"descriptor_bytes": raw, "profile": BASELINE},
    ):
        with pytest.raises(ValueError):
            import_corpus(
                tmp_path / "corpus",
                object(),
                "0" * 40,
                **(args | {"descriptor_sha256": digest(raw)} | changed),
            )
    for key, value in [
        ("audience", "family"),
        ("projection_profile", "unknown"),
        ("required_capabilities", ["unrecognized"]),
        ("schema_version", 2),
    ]:
        doc = {
            k: ([] if isinstance(v, list) and k != "required_capabilities" else v)
            for k, v in public.items()
        }
        doc[key] = value
        with pytest.raises(ValueError):
            validate_output(doc, "public", ENHANCED)
    invalid = copy.deepcopy(public)
    del invalid["required_capabilities"]
    with pytest.raises(ValueError):
        validate_output(invalid, "public", ENHANCED)


@pytest.fixture
def database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable PostgreSQL cluster")
    db = "contract_" + uuid.uuid4().hex
    roles = [db + "_p", db + "_f"]
    with psycopg.connect(url, autocommit=True) as cluster:
        cluster.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
        try:
            dsn = make_conninfo(url, dbname=db)
            with psycopg.connect(dsn, autocommit=True) as admin:
                serialized_migration(cluster, admin)
                for role, aud in zip(roles, ("public", "family"), strict=True):
                    admin.execute(
                        sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'workshop'").format(
                            sql.Identifier(role)
                        )
                    )
                    admin.execute(
                        sql.SQL("GRANT {} TO {}").format(
                            sql.Identifier("genealogy_" + aud + "_ro"), sql.Identifier(role)
                        )
                    )
                yield admin, [make_conninfo(dsn, user=r, password="workshop") for r in roles]
        finally:
            cluster.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(db)))
            for role in roles:
                cluster.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_http_and_sql_across_generations(tmp_path, database, request):
    inputs, person, asset = samples(tmp_path)
    admin, dsns = database
    store = PostgresStore(*dsns)
    session = {"approved": True}
    app = create_app(
        store=store,
        authorize_family=lambda _: session["approved"],
        portrait_root=tmp_path / "assets",
        asset_url=lambda *_: "https://example.invalid/forbidden",
    )
    if os.getenv("PORTRAIT_PROXY_TEST"):
        proxy = proxy_client(app, tmp_path)
        client = proxy.__enter__()
        request.addfinalizer(lambda: proxy.__exit__(None, None, None))
    else:
        client = TestClient(app)

    def stage(profile):
        docs = outputs(tmp_path, inputs, profile)
        runtime = BASELINE_SHA if profile == BASELINE else TARGET
        raw = canonical_bytes(descriptor(*docs, profile, runtime, inputs.digest))
        import_corpus(
            tmp_path / "corpus",
            admin,
            "1" * 40,
            publication_inputs=inputs,
            build_year=inputs.build_year,
            asset_root=tmp_path / "assets",
            profile=profile,
            descriptor_bytes=raw,
            descriptor_sha256=digest(raw),
            target_runtime=runtime,
        )
        for doc, dsn in zip(docs, dsns, strict=True):
            with psycopg.connect(dsn) as reader:
                schema = sql.Identifier("site_" + doc["audience"])
                rows = reader.execute(
                    sql.SQL("SELECT category,document FROM {}.entity").format(schema)
                ).fetchall()
                for category in (
                    "people",
                    "sources",
                    "assets",
                    "citations",
                    "assertions",
                    "history",
                    "alternatives",
                    "personas",
                    "narratives",
                    "places",
                ):
                    assert (
                        sorted((r for c, r in rows if c == category), key=lambda r: r["id"])
                        == doc[category]
                    )
        return docs

    old = os.getenv("BASELINE_READER_PATH")
    old_html = None
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy_inputs = materialize(legacy_root, historical=True)
    page = "/people/" + person
    if old:
        # Run the released producer and reader in their installed package.
        command = [sys.executable, "-m", "genealogy.cli"]
        env = {**os.environ, "PYTHONPATH": old}
        old_assets = legacy_root / "assets"
        old_assets.mkdir()
        for aud, doc in zip(("public", "family"), outputs(tmp_path, inputs, BASELINE), strict=True):
            path = legacy_root / (aud + ".json")
            subprocess.run(
                [
                    *command,
                    "compile",
                    str(legacy_root / "corpus"),
                    str(path),
                    "--inputs",
                    str(legacy_inputs),
                    "--asset-root",
                    str(old_assets),
                    "--audience",
                    aud,
                ],
                cwd=legacy_root,
                env=env,
                check=True,
            )
            assert path.read_bytes() == canonical_bytes(doc)
        subprocess.run(
            [
                *command,
                "import",
                str(legacy_root / "corpus"),
                make_conninfo(os.environ["TEST_DATABASE_URL"], dbname=admin.info.dbname),
                "--commit",
                "0" * 40,
                "--inputs",
                str(legacy_inputs),
                "--asset-root",
                str(old_assets),
            ],
            cwd=legacy_root,
            env=env,
            check=True,
        )
        old_html = client.get(page).content
    stage(BASELINE)
    baseline = client.get(page)
    assert baseline.status_code == 200 and "Favorite color" not in baseline.text
    url = "/portraits/family/" + asset["id"]
    assert client.get(url).status_code == 404
    old = os.getenv("BASELINE_READER_PATH")
    if old:
        # Execute the installed old reader in a clean interpreter against the same SQL views.
        script = "from genealogy.web.app import create_app; from genealogy.web.repository import PostgresStore; from starlette.testclient import TestClient; import sys; c=TestClient(create_app(store=PostgresStore(*sys.argv[1:3]),authorize_family=lambda _:True)); r=c.get(sys.argv[3]); assert r.status_code==200; assert c.get('/assets/'+sys.argv[4]).status_code==404; sys.stdout.buffer.write(r.content)"
        result = subprocess.check_output(
            [sys.executable, "-c", script, *dsns, page, asset["id"]],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": old},
        )
        assert result == baseline.content == old_html
    stage(ENHANCED)
    assert "teal &lt;blue&gt;" in client.get(page).text
    assert client.get(url).content == png()
    assert client.get(url, headers={"Range": "bytes=0-3"}).status_code == 206
    assert client.get("/assets/" + asset["id"]).status_code == 404
    for revoke in ("logout", "withdrawal"):
        if revoke == "logout":
            session["approved"] = False
        else:
            session["approved"] = True
            path = (
                tmp_path
                / "corpus"
                / "consent"
                / (identifier("consent", 601, historical=True) + ".json")
            )
            write_record(
                tmp_path / "corpus", json.loads(path.read_text()) | {"decision": "withdraw"}
            )
            stage(ENHANCED)
        assert url not in client.get(page).text
        for path in (url, url + "/thumbnail"):
            for method, headers in (
                ("GET", {}),
                ("HEAD", {}),
                ("GET", {"Range": "bytes=0-1"}),
                ("GET", {"If-None-Match": "*"}),
                ("GET", {"If-Modified-Since": "Thu, 01 Jan 2099 00:00:00 GMT"}),
            ):
                response = client.request(method, path, headers=headers)
                missing = client.request(method, "/portraits/family/missing", headers=headers)
                assert (
                    response.status_code,
                    {k: v for k, v in response.headers.items() if k != "date"},
                    response.content,
                ) == (
                    404,
                    {k: v for k, v in missing.headers.items() if k != "date"},
                    missing.content,
                )
                assert (
                    "no-store" in response.headers["cache-control"]
                    and "Cookie" in response.headers["vary"]
                )
                assert not {"etag", "last-modified", "location"} & set(response.headers)
