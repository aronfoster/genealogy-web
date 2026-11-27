"""Render synthetic person cards through production templates for visual/a11y review."""

from __future__ import annotations

import argparse
import json
import socket
import threading
from pathlib import Path

import uvicorn
from browser_inputs import run_browser
from starlette.responses import HTMLResponse
from starlette.routing import Route

from genealogy.indicators import name_rules
from genealogy.web.app import TEMPLATES, create_app
from genealogy.web.person_indicators import attributes, membership
from genealogy.web.repository import Record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    rules = name_rules()
    examples = [
        ("Established ancestor", "tree", False, "adult", "Parent of Example Relative"),
        ("Spouse", "tree", False, "adult", "Spouse of Example Relative"),
        ("Adopted child", "tree", False, "child", "Adoptive child of Example Relative"),
        ("Collateral relative", "tree", False, "adult", "Sibling of Example Relative"),
        ("Possible connection", "possible", False, "unknown", "Connection remains unresolved"),
        ("Documented child", "other", True, "child", "Enslavement record; no established kinship"),
        ("Ledger-only person", "other", False, None, "Named in a ledger"),
        ("Guardian", "other", False, None, "Guardianship connection"),
        ("Unknown attributes", "other", False, None, "Age and life status not established"),
    ]
    cards: list[Record] = []
    for i, (name, scope, slave, stage, context) in enumerate(examples):
        person: Record = {"family_scope": scope, "slave": slave}
        claims = (
            [
                {
                    "id": f"claim-{i}",
                    "subject_id": f"per_synthetic_{i}",
                    "assertion_type": "person.death",
                    "collection": "assertions",
                    "status": "accepted",
                    "value": {"date": {"raw": "1900"}, "life_stage": stage},
                }
            ]
            if stage
            else []
        )
        cards.append(
            {
                "id": f"per_synthetic_{i}",
                "kind": "Person",
                "name": name,
                "membership": membership(person),
                "attributes": attributes(person, claims, rules=rules),
                "name_context": context,
            }
        )
    extra_examples: list[tuple[str, str, list[tuple[str, Record]]]] = [
        (
            "William Example",
            "tree",
            [
                ("person.name", {"raw": "William Example"}),
                (
                    "person.birth",
                    {
                        "date": {
                            "raw": "1900",
                            "normalized": "1900-04-17",
                            "calendar": "gregorian",
                        },
                        "raw_place": "United States",
                    },
                ),
            ],
        ),
        (
            "Mary Example",
            "tree",
            [
                ("person.name", {"raw": "Mary Example"}),
                (
                    "person.birth",
                    {
                        "date": {
                            "raw": "1900",
                            "normalized": "1900-04-17",
                            "calendar": "gregorian",
                        },
                        "raw_place": "United States",
                    },
                ),
            ],
        ),
        (
            "Living adult",
            "tree",
            [
                ("person.sex", {"sex": "female"}),
                ("person.living", {"as_of": "2026-03-17", "valid_through": "2028-04-23"}),
            ],
        ),
        (
            "Living minor",
            "tree",
            [
                ("person.sex", {"sex": "male"}),
                ("person.living", {"as_of": "2026-03-17", "valid_through": "2028-04-23"}),
                (
                    "person.birth",
                    {"date": {"raw": "2019", "normalized": "2019-01-01", "calendar": "gregorian"}},
                ),
            ],
        ),
    ]
    for name, scope, raw_claims in extra_examples:
        rid = f"per_synthetic_{len(cards)}"
        claims = [
            {
                "id": f"{rid}-{j}",
                "subject_id": rid,
                "assertion_type": typ,
                "value": value,
                "collection": "assertions",
                "status": "accepted",
            }
            for j, (typ, value) in enumerate(raw_claims)
        ]
        person = {"id": rid, "family_scope": scope}
        cards.append(
            {
                "id": rid,
                "kind": "Person",
                "name": name,
                "membership": membership(person),
                "attributes": attributes(person, claims, build_year=2026, rules=rules),
            }
        )
    template = TEMPLATES.from_string("""{% extends "base.html" %}
{% from "macros.html" import person %}{% from "person_indicators.html" import legend %}
{% block content %}<h1>{{ title }}</h1><p>{{ review_note }}</p>
<h2>Membership and documented attributes</h2><div class="cards">{% for card in cards %}{{ person(card) }}{% endfor %}</div>{{ legend() }}{% endblock %}""")
    app = create_app(store=object())
    app.routes.insert(
        0,
        Route(
            "/indicator-review",
            lambda _: HTMLResponse(
                template.render(
                    title="Person card review",
                    cards=cards,
                    review_note="Synthetic examples — no real family data.",
                )
            ),
        ),
    )
    app.routes.insert(
        0,
        Route(
            "/indicator-profile",
            lambda _: HTMLResponse(
                TEMPLATES.get_template("page.html").render(
                    title=cards[5]["name"],
                    view="person",
                    pagination={},
                    data={**cards[5], "relationships": {}, "has_family": False},
                )
            ),
        ),
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            run_browser(
                repo / "scripts/check_person_indicators.mjs",
                json.dumps({"actual": [], "reviewed": []}).encode(),
                {
                    "INDICATOR_ACTUAL": "",
                    "INDICATOR_TEST_URL": f"http://127.0.0.1:{sock.getsockname()[1]}",
                },
                None,
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
