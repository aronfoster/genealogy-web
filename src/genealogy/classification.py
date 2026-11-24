"""Audience-local browsing classifications; never inspect the private graph."""

from __future__ import annotations

from typing import Any


def _walk(seeds: set[str], edges: list[tuple[str, str]]) -> set[str]:
    reached = set(seeds)
    while True:
        added = {b for a, b in edges if a in reached} - reached
        if not added:
            return reached
        reached.update(added)


def classify_people(document: dict[str, Any], roots: set[str] | frozenset[str]) -> None:
    people = {p["id"] for p in document["people"]}
    roots = set(roots) & people
    current = document["assertions"]
    parents: list[tuple[str, str]] = []
    spouses: list[tuple[str, str]] = []
    identity_edges: dict[str, set[str]] = {}
    # Connected components use only current, permitted, confirmed identity joins.
    for c in current:
        if (
            c["assertion_type"] == "identity.same_person"
            and c["value"]["identity_state"] == "confirmed"
        ):
            a, b = c["subject_id"], c["value"]["other_id"]
            identity_edges.setdefault(a, set()).add(b)
            identity_edges.setdefault(b, set()).add(a)
    identities: dict[str, set[str]] = {p: {p} for p in people}
    unseen = set(identity_edges)
    while unseen:
        pending = [next(iter(unseen))]
        component: set[str] = set()
        while pending:
            subject = pending.pop()
            if subject in component:
                continue
            component.add(subject)
            pending.extend(identity_edges.get(subject, set()) - component)
        unseen.difference_update(component)
        for subject in component:
            identities[subject] = component & people
    for c in current:
        v = c["value"]
        if (
            c["assertion_type"] == "relationship.parent_child"
            and v["relationship_kind"] != "guardian"
        ):
            if v["parent_id"] in people and v["child_id"] in people:
                parents.append((v["parent_id"], v["child_id"]))
        elif c["assertion_type"] == "relationship.spouse":
            if c["subject_id"] in people and v["other_id"] in people:
                spouses.append((c["subject_id"], v["other_id"]))

    def family(edges: list[tuple[str, str]], marriages: list[tuple[str, str]]) -> set[str]:
        ancestors = _walk(roots, [(b, a) for a, b in edges])
        relatives = _walk(ancestors, edges)
        return relatives | {
            b for a, b in marriages + [(b, a) for a, b in marriages] if a in relatives
        }

    tree = family(parents, spouses)
    possible: set[str] = set()
    uncertain = document["alternatives"] + [
        c
        for c in current
        if c["assertion_type"] == "identity.same_person"
        and c["value"]["identity_state"] in {"possible", "probable"}
    ]
    for c in uncertain:
        v = c["value"]
        if (
            c["assertion_type"] == "relationship.parent_child"
            and v["relationship_kind"] != "guardian"
        ):
            a, b = v["parent_id"], v["child_id"]
            if a in people and b in people:
                possible.update(family(parents + [(a, b)], spouses) - tree)
        elif c["assertion_type"] == "relationship.spouse":
            a, b = c["subject_id"], v["other_id"]
            if a in people and b in people:
                possible.update(family(parents, spouses + [(a, b)]) - tree)
        elif c["assertion_type"] == "identity.same_person" and v["identity_state"] != "rejected":
            left = identities.get(c["subject_id"], set())
            right = identities.get(v["other_id"], set())
            if left & tree:
                possible.update(right - tree)
            if right & tree:
                possible.update(left - tree)
    slaves = set()
    for c in current:
        if c["assertion_type"] == "historical.enslavement":
            slaves.update(identities.get(c["subject_id"], set()))
    for p in document["people"]:
        p["family_scope"] = (
            "tree" if p["id"] in tree else "possible" if p["id"] in possible else "other"
        )
        p["slave"] = p["id"] in slaves
