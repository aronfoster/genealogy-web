"""Regression contracts for the invented workshop matrix and its reader oracles."""

import argparse
import hashlib

from browser_inputs import ScenarioInputs
from check_browser_suite import VARIANTS
from check_synthetic_browser import SCENARIOS, materialize

from genealogy.input_options import input_options, load_publication_inputs, validate_corpus
from genealogy.publication import compile_publications
from genealogy.publication_inputs import strict_json


def test_workshop_history_freeze(tmp_path):
    materialize(tmp_path, historical=True)
    digest = hashlib.sha256()
    for path in sorted((tmp_path / "corpus").rglob("*.json")):
        digest.update(path.relative_to(tmp_path).as_posix().encode() + b"\0" + path.read_bytes())
    assert digest.hexdigest() == "04f3cdc23f619e629f65986cb8a45469c413e1ba56013b6e0b22bc872b552b5e"
    expected = strict_json(ScenarioInputs.load(SCENARIOS / "historical.json", "historical").raw)
    assert strict_json((tmp_path / "record-map.json").read_bytes()) == {
        k: expected[k] for k in ("senior", "child", "infant")
    }


def test_workshop_audience_and_overlay_controls(tmp_path):
    assert set(VARIANTS) == {
        "public",
        "family",
        "source-preview",
        "names-public",
        "names-family",
        "historical",
        "auth",
        "prototype",
        "indicators",
    }
    args = argparse.Namespace(inputs=materialize(tmp_path, historical=False))
    input_options(args)
    inputs = load_publication_inputs(args)
    _, records = validate_corpus(args.corpus, inputs, args.asset_root)
    public, family, _ = compile_publications(records, inputs=inputs)
    current = strict_json(ScenarioInputs.load(SCENARIOS / "current.json", "current").raw)
    auth = strict_json(ScenarioInputs.load(SCENARIOS / "auth.json", "auth").raw)
    assert auth["senior"] in {person["id"] for person in family["people"]}
    for projection, approved in ((public, False), (family, True)):
        names = {
            claim["value"]["raw"]
            for claim in projection["assertions"]
            if claim["assertion_type"] == "person.name"
        }
        assert (auth["livingName"] in names) == approved
        assert ("The Ember Scribe" in names) == approved
        assert (auth["living"] in {p["id"] for p in projection["people"]}) == approved
        assert (auth["asset"] in {a["id"] for a in projection["assets"]}) == approved
        assert current["preferredName"] not in names
        assert not any(current["previewQuery"] in name for name in names)
        assert not any(c.get("locations") for c in projection["citations"])
        assert {p["family_scope"] for p in projection["people"]} >= {"other", "tree"}
    assert {
        c["value"]["relationship_kind"]
        for c in public["assertions"]
        if c["assertion_type"] == "relationship.parent_child"
    } >= {"biological", "adoptive", "guardian", "step"}
    assert public["alternatives"]
