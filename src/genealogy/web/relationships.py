"""Kinship derived exclusively from one authorized publication, never raw corpus data."""

from __future__ import annotations

from collections.abc import Callable

from .repository import Record, Repository

SIBLING_KINDS = {"biological", "adoptive", "unknown"}


def parent_label(kind: str, role: str) -> str:
    if kind == "unknown":
        return role.capitalize() + " (type of relationship not recorded)"
    return kind.capitalize() + " " + role


def relationships(
    repo: Repository,
    person_id: str,
    card: Callable[[str], Record],
    claim: Callable[[Record], Record],
    *,
    include_siblings: bool = True,
    include_claims: bool = True,
) -> Record:
    """Keep accepted kinship, alternatives and non-kinship connections distinct."""
    groups: dict[str, list[Record]] = {
        k: [] for k in ("Parents", "Spouses", "Siblings", "Children")
    }
    uncertain: list[Record] = []
    other_connections: list[Record] = []
    claims = [c for c in repo.claims([person_id]) if c["collection"] != "history"]
    parents = []

    def add(
        other: str,
        group: str,
        label: str,
        supporting: list[Record],
        kind: str,
        *,
        qualified: bool = False,
        nonkin: bool = False,
        shared_parent: str | None = None,
    ) -> None:
        if other == person_id or not repo.get(other, ["people"]):
            return
        unresolved = any(c["collection"] != "assertions" for c in supporting)
        tier = "uncertain" if qualified or unresolved else "accepted"
        if unresolved:
            label = "Possible " + label[0].lower() + label[1:]
        destination = (
            other_connections if nonkin else uncertain if tier == "uncertain" else groups[group]
        )
        # Group remains part of the key: one person can legitimately have two roles.
        row = next(
            (
                r
                for r in destination
                if r["id"] == other and r["group"] == group and r["status_tier"] == tier
            ),
            None,
        )
        if row is None:
            row = {
                **card(other),
                "group": group,
                "status_tier": tier,
                "kinds": [],
                "labels": [],
                "claims": [],
                "shared_parents": [],
            }
            destination.append(row)
        if label not in row["labels"]:
            row["labels"].append(label)
        if kind not in row["kinds"]:
            row["kinds"].append(kind)
        if shared_parent and shared_parent not in row["shared_parents"]:
            row["shared_parents"].append(shared_parent)
        if not include_claims:
            return
        known = {c["id"] for c in row["claims"]}
        for c in supporting:
            if c["id"] not in known:
                row["claims"].append(claim(c))
                known.add(c["id"])

    for c in claims:
        v, typ = c["value"], c["assertion_type"]
        if typ == "relationship.parent_child":
            if person_id not in (v["parent_id"], v["child_id"]):
                continue
            is_child = v["child_id"] == person_id
            other = v["parent_id"] if is_child else v["child_id"]
            kind = v["relationship_kind"]
            if kind == "guardian":
                add(
                    other,
                    "Guardianship",
                    "Guardian" if is_child else "Ward",
                    [c],
                    kind,
                    nonkin=True,
                )
                continue
            add(
                other,
                "Parents" if is_child else "Children",
                parent_label(kind, "parent" if is_child else "child"),
                [c],
                kind,
            )
            if is_child and kind in SIBLING_KINDS and repo.get(other, ["people"]):
                parents.append(c)
        elif typ == "identity.same_person":
            # Identity candidates connect distinct profiles; never turn them into
            # a parent/spouse edge or traverse them to infer additional relatives.
            if v.get("identity_state") == "rejected":
                continue
            other = v["other_id"] if c["subject_id"] == person_id else c["subject_id"]
            confirmed = c["collection"] == "assertions" and v["identity_state"] == "confirmed"
            add(
                other,
                "Identity",
                "Same person (confirmed identity)"
                if confirmed
                else v["identity_state"].capitalize() + " identity match"
                if c["collection"] == "assertions"
                else "Identity match",
                [c],
                "identity",
                qualified=not confirmed,
                nonkin=confirmed,
            )
        elif typ in {"relationship.spouse", "relationship.guardian", "relationship.adoption"}:
            other = v["other_id"] if c["subject_id"] == person_id else c["subject_id"]
            if other == person_id or not repo.get(other, ["people"]):
                continue
            if typ == "relationship.spouse":
                label = "Spouse"
                if v.get("date"):
                    label += " · " + claim(c)["date"]["text"]
                add(other, "Spouses", label, [c], "spouse")
            elif typ == "relationship.guardian":
                # This schema does not establish which endpoint is the guardian.
                add(other, "Guardianship", "Guardianship connection", [c], "guardian", nonkin=True)
            else:
                # An adoption statement is not a directional parent-child edge.
                add(
                    other,
                    "Adoption",
                    "Adoption connection (roles not specified)",
                    [c],
                    "adoption",
                    qualified=True,
                )

    for parent in parents if include_siblings else []:
        parent_id = parent["value"]["parent_id"]
        for c in repo.claims([parent_id]):
            v = c["value"]
            if (
                c["assertion_type"] != "relationship.parent_child"
                or c["collection"] == "history"
                or v["parent_id"] != parent_id
                or v["relationship_kind"] not in SIBLING_KINDS
            ):
                continue
            label = "Sibling (shares " + card(parent_id)["name"] + ")"
            if parent["collection"] != "assertions":
                label = "Sibling (through possible parent " + card(parent_id)["name"] + ")"
            elif c["collection"] != "assertions":
                label = "Sibling (possible child of " + card(parent_id)["name"] + ")"
            add(v["child_id"], "Siblings", label, [parent, c], "sibling", shared_parent=parent_id)

    def biological_parents(child: str) -> dict[str, list[Record]]:
        result: dict[str, list[Record]] = {}
        for c in repo.claims([child]):
            v = c["value"]
            if (
                c["assertion_type"] == "relationship.parent_child"
                and c["collection"] == "assertions"
                and v["child_id"] == child
                and v["relationship_kind"] == "biological"
                and repo.get(v["parent_id"], ["people"])
            ):
                result.setdefault(v["parent_id"], []).append(c)
        return result

    def identity_connected(start: str, target: str) -> bool:
        pending, visited = [start], set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            for c in repo.claims([current]):
                if c["assertion_type"] == "identity.same_person" and c["collection"] != "history":
                    pending.append(
                        c["value"]["other_id"] if c["subject_id"] == current else c["subject_id"]
                    )
        return False

    focus_parents = biological_parents(person_id) if groups["Siblings"] else {}
    for row in groups["Siblings"]:
        names = sorted((card(p)["name"] for p in row["shared_parents"]), key=str.casefold)
        row["labels"] = ["Sibling (shares " + " and ".join(names) + ")"]
        sibling_parents = biological_parents(row["id"])
        shared = focus_parents.keys() & sibling_parents.keys()
        if len(focus_parents) == len(sibling_parents) == 2 and len(shared) == 1:
            left = next(iter(focus_parents.keys() - shared))
            right = next(iter(sibling_parents.keys() - shared))
            if not identity_connected(left, right):
                shared_name = card(next(iter(shared)))["name"]
                row["labels"] = ["Half-sibling (shares biological parent " + shared_name + ")"]
                # Retain the evidence establishing both different other parents.
                known = {c["id"] for c in row["claims"]}
                for c in (focus_parents[left] + sibling_parents[right]) if include_claims else []:
                    if c["id"] not in known:
                        row["claims"].append(claim(c))
                        known.add(c["id"])

    buckets = [*groups.values(), uncertain, other_connections]
    for rows in buckets:
        for row in rows:
            row["relationship"] = "; ".join(row.pop("labels"))
            row["claims"].sort(key=lambda c: c["id"])
        rows.sort(key=lambda r: (r["name"].lower(), r["id"], r["group"], r["status_tier"]))
    return {
        "focus": card(person_id),
        "groups": {k: v for k, v in groups.items() if v},
        "uncertain": uncertain,
        "other_connections": other_connections,
    }
