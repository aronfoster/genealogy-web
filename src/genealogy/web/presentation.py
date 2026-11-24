"""Reader models built solely from an already authorized repository."""

from __future__ import annotations

import re
from collections import Counter
from itertools import combinations
from urllib.parse import urlsplit

from ..evidence_urls import safe_evidence_url
from ..names import NAME_LABELS, select_name
from .card_dates import card_dates
from .errors import NotFound, ProjectionInvariantError
from .person_indicators import attributes, membership
from .relationships import relationships
from .repository import Record, Repository
from .urls import record_url

STATUS = {
    "accepted": "Current conclusion",
    "candidate": "Still being resolved",
    "conflicting": "Sources disagree",
    "rejected": "Corrected claim",
    "corrected": "Corrected claim",
}
ORIGIN = {
    "source_statement": "Stated by source",
    "researcher_conclusion": "Researcher conclusion",
    "researcher_derivation": "Calculated by researcher",
    "family_testimony": "Family testimony",
}
GRADE = {
    "A": "Strong evidence",
    "B": "Moderate evidence",
    "C": "Limited evidence",
    "D": "Early lead",
}
LABELS = {
    "person.name": "Name",
    "person.birth": "Birth",
    "person.death": "Death",
    "person.sex": "Recorded sex",
    "person.living": "Living status",
    "identity.same_person": "Identity connection",
    "identity.not_same_person": "Separate identities",
    "relationship.parent_child": "Parent and child",
    "relationship.spouse": "Marriage / spouse",
    "relationship.guardian": "Guardian",
    "relationship.adoption": "Adoption relationship",
    "event.adoption": "Adoption",
    "event.marriage": "Marriage",
    "person.residence": "Residence",
    "person.occupation": "Occupation",
    "person.education": "Education",
    "person.military_service": "Military service",
    "source.interpretation": "Source interpretation",
    "event.recorded_property": "Property record",
    "historical.enslavement": "Enslavement record",
}


def date_text(date: Record) -> str:
    raw = str(date.get("raw") or "Date not yet known")
    precision = date.get("precision", "exact")
    if precision in {"about", "before", "after", "between"} and not re.search(
        rf"\b{precision}\b", raw, re.I
    ):
        raw = f"{precision.capitalize()} {raw}"
    return raw


def locator_segments(locator: str) -> list[Record]:
    """Replace safe URLs in place; retain every occurrence and surrounding character."""
    matches = []
    for match in re.finditer(r"https?://[^\s<>\"'|]+", locator):
        url = match.group()
        while True:
            trimmed = url.rstrip(".,;")
            if trimmed.endswith(")") and trimmed.count(")") > trimmed.count("("):
                trimmed = trimmed[:-1]
            if trimmed == url:
                break
            url = trimmed
        if safe_evidence_url(url):
            matches.append((match.start(), url, urlsplit(url).hostname))
    counts = Counter(host for _, _, host in matches)
    seen: Counter[str | None] = Counter()
    result: list[Record] = []
    cursor = 0
    for start, url, host in matches:
        if start > cursor:
            result.append({"text": locator[cursor:start]})
        seen[host] += 1
        label = f"{host} record"
        if counts[host] > 1:
            label += f" ({seen[host]} of {counts[host]})"
        result.append({"url": url, "label": label})
        cursor = start + len(url)
    if cursor < len(locator):
        result.append({"text": locator[cursor:]})
    return result


class Presenter:
    def __init__(self, repository: Repository, *, hosted_copies_enabled: bool = False) -> None:
        self.repo = repository
        self.hosted_copies_enabled = hosted_copies_enabled
        self._cards: dict[str, Record] = {}
        # A Presenter belongs to one audience and one read-only transaction.
        self._citations: dict[str, Record] = {}
        self._claims: dict[str, Record] = {}

    def card(self, person_id: str) -> Record:
        if person_id in self._cards:
            return self._cards[person_id]
        person = self.repo.get(person_id, ["people", "personas"])
        if person is None:
            raise NotFound
        claims = self.repo.claims([person_id])
        own = [c for c in claims if c["subject_id"] == person_id]
        name_claims = [
            c
            for c in own
            if c["assertion_type"] == "person.name" and c["collection"] == "assertions"
        ]
        names = list(dict.fromkeys(c["value"]["raw"] for c in name_claims))
        selected_id = person.get("display_name_assertion_id")
        selected = next((c for c in name_claims if c["id"] == selected_id), None)
        if selected_id and selected is None:
            raise ProjectionInvariantError
        # Compatibility for record subjects and pre-extension imported projections.
        if not selected_id:
            selected = select_name(name_claims)
        is_persona = "raw_name" in person
        card: Record = {
            "id": person_id,
            "name": selected["value"]["raw"]
            if selected
            else person.get("raw_name", "Name not yet established"),
            "kind": "Record subject" if is_persona else "Person",
            "dates": [],
            "places": [],
        }
        card["compact_dates"] = card_dates(own)
        card["membership"] = membership(person)
        card["attributes"] = (
            person["indicators"] if "indicators" in person else attributes(person, own)
        )
        card["badges"] = []
        if not is_persona and "family_scope" in person:
            card["badges"].append(
                {
                    "tree": "Family tree",
                    "possible": "Possible family connection",
                    "other": "Other person in records",
                }[person["family_scope"]]
            )
        if person.get("slave"):
            card["badges"].append("Slave")
        card["badges"].extend(a["label"] for a in card["attributes"] if a["icon"] == "child")
        card["card_place"] = next(
            (
                prefix + c["value"]["raw_place"]
                for typ, prefix in (("person.residence", "Lived in "), ("person.birth", "Born in "))
                for c in own
                if c["assertion_type"] == typ
                and c["collection"] == "assertions"
                and c["value"].get("raw_place")
            ),
            "",
        )
        card["other_names"] = [n for n in names if n != card["name"]]
        card["name_details"] = [
            {
                "id": c["id"],
                "name": c["value"]["raw"],
                "label": NAME_LABELS[c["value"].get("name_kind", "unknown")],
                "preferred": bool(c["value"].get("preferred_display")),
                "context": c["value"].get("context", ""),
                "usage": [
                    {"label": label, "text": date_text(c["value"][key])}
                    for key, label in (("used_from", "Used from"), ("used_to", "Used until"))
                    if key in c["value"]
                ],
            }
            for c in sorted(name_claims, key=lambda c: (c["value"]["raw"].casefold(), c["id"]))
        ]
        card["name_context"] = selected["value"].get("context", "") if selected else ""
        for event in ("person.birth", "person.death"):
            events = [
                c for c in own if c["assertion_type"] == event and c["collection"] != "history"
            ]
            if events:
                card["dates"].append(
                    {
                        "label": LABELS[event],
                        "text": " or ".join(
                            dict.fromkeys(date_text(c["value"]["date"]) for c in events)
                        ),
                        "uncertain": any(c["collection"] == "alternatives" for c in events),
                        "status": "Sources disagree"
                        if any(c["status"] == "conflicting" for c in events)
                        else "Still being resolved"
                        if any(c["collection"] == "alternatives" for c in events)
                        else "Current conclusion",
                    }
                )
                card["places"].extend(
                    c["value"]["raw_place"] for c in events if c["value"].get("raw_place")
                )
        card["places"] = list(dict.fromkeys(card["places"]))
        self._cards[person_id] = card
        return card

    def citation(self, citation_id: str) -> Record:
        if citation_id not in self._citations:
            self._citations[citation_id] = self._citation(citation_id)
        return self._citations[citation_id]

    def _citation(self, citation_id: str) -> Record:
        citation = self.repo.get(citation_id, ["citations"])
        if citation is None:
            raise ProjectionInvariantError
        source = self.repo.get(citation["source_id"], ["sources"])
        if source is None:
            raise ProjectionInvariantError
        locations = []
        for location in citation.get("locations", []):
            item = dict(location)
            if item["kind"] == "hosted_copy":
                asset = self.repo.get(item["asset_id"], ["assets"])
                if not asset or not asset["bytes_available"] or asset["source_id"] != source["id"]:
                    continue
                if self.hosted_copies_enabled:
                    item["url"] = record_url("assets", asset["id"])
                item["kind_label"] = "Site-hosted copy"
            else:
                item["kind_label"] = {
                    "external_original": "External original record",
                    "external_index": "External index",
                    "external_publication": "External publication",
                }[item["kind"]]
                if item.get("url"):
                    if not safe_evidence_url(item["url"]):
                        raise ProjectionInvariantError
                    item["host"] = urlsplit(item["url"]).hostname
            locations.append(item)
        return {
            **{k: citation[k] for k in ("id", "source_id", "locator", "inspection")},
            **({"transcription": citation["transcription"]} if "transcription" in citation else {}),
            "source": self.source_card(source),
            "locations": locations,
            "segments": locator_segments(citation["locator"]),
        }

    @staticmethod
    def source_card(source: Record) -> Record:
        return {
            k: source[k]
            for k in ("id", "title", "source_kind", "reader_description")
            if k in source
        }

    def reference(self, record_id: str) -> Record:
        if self.repo.get(record_id, ["people", "personas"]):
            card = self.card(record_id)
            section = "records" if card["kind"] == "Record subject" else "people"
            return {**card, "url": record_url(section, record_id)}
        source = self.repo.get(record_id, ["sources"])
        if source:
            return {
                "id": record_id,
                "name": source["title"],
                "kind": "Source",
                "url": record_url("sources", record_id),
            }
        for category, kind, name_key in (
            ("citations", "Citation", "locator"),
            ("assets", "Source material", "label"),
        ):
            item = self.repo.get(record_id, [category])
            if item:
                if not self.repo.get(item["source_id"], ["sources"]):
                    raise ProjectionInvariantError
                return {
                    "id": record_id,
                    "name": item[name_key],
                    "kind": kind,
                    "url": record_url("sources", item["source_id"], record_id),
                }
        # Publication removes the entire claim when a content endpoint is hidden.
        # Partial rendering here could disclose its text or the relationship itself.
        raise ProjectionInvariantError

    def claim(self, claim: Record) -> Record:
        if claim["id"] not in self._claims:
            self._claims[claim["id"]] = self._claim(claim)
        return self._claims[claim["id"]]

    def _claim(self, claim: Record) -> Record:
        value = claim["value"]
        assertion_type = claim["assertion_type"]
        result = {
            "id": claim["id"],
            "label": LABELS.get(assertion_type, "Historical claim"),
            "status": "Corrected claim"
            if claim.get("collection") == "history"
            else STATUS[claim["status"]],
            "collection": claim.get("collection", "assertions"),
            "text": value.get("raw", ""),
            "references": [],
        }
        if assertion_type == "person.name":
            result["label"] = NAME_LABELS[value.get("name_kind", "unknown")]
            result["name_context"] = value.get("context", "")
            result["name_usage"] = [
                {"label": label, "text": date_text(value[key])}
                for key, label in (("used_from", "Used from"), ("used_to", "Used until"))
                if key in value
            ]
        if assertion_type == "person.sex":
            result["text"] = value["sex"].capitalize() + " (recorded sex)"
        if assertion_type == "person.living":
            result["text"] = (
                "Reported living as of "
                + value["as_of"]
                + "; revalidate by "
                + value["valid_through"]
            )
        if "date" in value:
            result["date"] = {**value["date"], "text": date_text(value["date"])}
        if "raw_place" in value:
            result["place"] = value["raw_place"]
        elif "place_id" in value:
            place = self.repo.get(value["place_id"], ["places"])
            if place:
                result["place"] = place["label"]
        if "relationship_kind" in value:
            result["text"] = value["relationship_kind"].capitalize() + " relationship"
            if value["relationship_kind"] == "guardian":
                result["label"] = "Guardianship"
                result["text"] = "Guardianship connection"
        if "identity_state" in value:
            result["text"] = value["identity_state"].capitalize() + " identity connection"
            if assertion_type == "identity.not_same_person":
                result["text"] = "Separate people — " + value["identity_state"]
        if assertion_type == "historical.enslavement":
            if result["text"] and claim.get("evidence"):
                result["historical_claim_detail"] = result["text"]
            # Keep sale/inventory wording out of the primary timeline even when
            # publication removes all citation evidence for this audience.
            result["text"] = "This claim records an enslavement-related entry."
        references = {claim["subject_id"]: "Subject"}
        for key in ("parent_id", "child_id", "other_id"):
            if key in value:
                role = (
                    "Other person named in this record"
                    if assertion_type == "historical.enslavement"
                    else key.removesuffix("_id").capitalize()
                )
                if (
                    assertion_type == "relationship.parent_child"
                    and value.get("relationship_kind") == "guardian"
                ):
                    role = {"parent_id": "Guardian", "child_id": "Ward"}.get(key, role)
                elif assertion_type == "relationship.guardian" and key == "other_id":
                    role = "Other person named in this guardianship record"
                references[value[key]] = role
        result["references"] = [
            {"role": role, **self.reference(rid)} for rid, role in references.items()
        ]
        if result["references"]:
            result["link_label"] = result["references"][0]["name"] + " — " + result["label"]
        # A public claim with no permitted citations is ordinary prose: no empty
        # evidence card, score, hidden count or invitation to unlock anything.
        if claim.get("evidence"):
            result["evidence"] = [
                {
                    **self.citation(e["citation_id"]),
                    "stance": e["stance"],
                    "explicitness": e["explicitness"],
                }
                for e in claim["evidence"]
            ]
            result["origin"] = ORIGIN.get(claim["origin"], claim["origin"].replace("_", " "))
            result["grade"] = GRADE.get(claim.get("grade", ""), "Not graded")
            if claim.get("grade"):
                result["grade"] += " (" + claim["grade"] + ")"
        return result

    def profile(self, person_id: str) -> Record:
        card = self.card(person_id)
        claims = self.repo.claims([person_id])
        profile: Record = {
            **card,
            "current": [],
            "alternatives": [],
            "history": [],
            "records": [],
            "narratives": [],
        }
        for claim in sorted(
            claims, key=lambda c: (c["value"].get("date", {}).get("normalized") or "9999", c["id"])
        ):
            key = "current" if claim["collection"] == "assertions" else claim["collection"]
            profile[key].append(self.claim(claim))
            if (
                claim["collection"] == "assertions"
                and claim["assertion_type"] == "identity.same_person"
            ):
                other = (
                    claim["value"]["other_id"]
                    if claim["subject_id"] == person_id
                    else claim["subject_id"]
                )
                persona = self.repo.get(other, ["personas"])
                if persona:
                    profile["records"].append(
                        {**self.card(other), "identity_state": claim["value"]["identity_state"]}
                    )
        for narrative in self.repo.narratives(person_id):
            # Plain text and explicit assertion links; corpus Markdown is never HTML.
            profile["narratives"].append(
                {
                    "id": narrative["id"],
                    "title": narrative["title"],
                    "body": re.sub(r"\[\[asn_[0-9a-f-]+\]\]", "", narrative["body"]),
                    "claims": [
                        self.claim(c)
                        for rid in narrative["assertions"]
                        if (c := self.repo.get(rid, ["assertions", "alternatives", "history"]))
                    ],
                }
            )
        if card["kind"] == "Person":
            profile["relationships"] = self.family(person_id)
            profile["has_family"] = bool(
                profile["relationships"]["groups"] or profile["relationships"]["uncertain"]
            )
        if card["kind"] == "Record subject":
            persona = self.repo.get(person_id, ["personas"])
            if persona is None:
                raise ProjectionInvariantError
            profile["record_citation"] = self.citation(persona["citation_id"])
        return profile

    def family(self, person_id: str) -> Record:
        focus = self.card(person_id)
        if focus["kind"] != "Person":
            raise NotFound
        model = relationships(self.repo, person_id, self.card, self.claim)
        rows = [r for relatives in model["groups"].values() for r in relatives]
        rows += model["uncertain"] + model["other_connections"]
        # When names and life dates collide, retain a supported record date as
        # additional context. A record date never becomes a birth/death assertion.
        for row in rows:
            if row["places"]:
                continue
            collisions = [
                r
                for r in rows
                if r["id"] != row["id"] and r["name"] == row["name"] and r["dates"] == row["dates"]
            ]
            if collisions:
                dated = [
                    c
                    for c in self.repo.claims([row["id"]])
                    if c["collection"] != "history"
                    and c["assertion_type"] not in {"person.birth", "person.death"}
                    and c["value"].get("date")
                ]
                if dated:
                    dated.sort(key=lambda c: c["id"])
                    row["recorded"] = {
                        "text": LABELS.get(dated[0]["assertion_type"], "Dated claim")
                        + ": "
                        + date_text(dated[0]["value"]["date"])
                        + (
                            " (" + STATUS[dated[0]["status"]] + ")"
                            if dated[0]["collection"] == "alternatives"
                            else ""
                        ),
                        "url": record_url("assertions", dated[0]["id"]),
                    }
        return model

    def browsing_card(self, person_id: str, query: str = "") -> Record:
        card = {**self.card(person_id)}
        if query.strip():
            tokens = query.lower().split()
            own = self.repo.claims([person_id])
            subjects = {person_id}
            for c in own:
                if (
                    c["assertion_type"] == "identity.same_person"
                    and c["collection"] == "assertions"
                ):
                    subjects.update((c["subject_id"], c["value"]["other_id"]))
            matches = []
            coverage: dict[str, set[str]] = {}
            for c in self.repo.claims(sorted(subjects)):
                if (
                    c["subject_id"] not in subjects
                    or c["assertion_type"] != "person.name"
                    or c["collection"] not in {"assertions", "alternatives"}
                ):
                    continue
                value = c["value"]
                words = (value["raw"] + " " + value.get("normalized", "")).lower()
                if any(t in words for t in tokens):
                    coverage[c["id"]] = {t for t in tokens if t in words}
                    matches.append(
                        {
                            "id": c["id"],
                            "name": value["raw"],
                            "status": STATUS[c["status"]],
                            "record_name": c["subject_id"] != person_id,
                        }
                    )
            matches.sort(
                key=lambda n: (
                    n["record_name"],
                    n["status"] != "Current conclusion",
                    n["name"].casefold(),
                    n["id"],
                )
            )
            # Maximize token coverage before the existing evidence/name tie-break.
            # Consider pairs together: a greedy first choice can hide a token.
            choices = [(a, b) for a, b in combinations(matches, 2) if a["name"] != b["name"]] + [
                (a,) for a in matches
            ]
            card["matched_names"] = list(
                max(
                    choices,
                    key=lambda pair: len(set().union(*(coverage[n["id"]] for n in pair))),
                    default=(),
                )
            )
        model = relationships(
            self.repo,
            person_id,
            self.card,
            self.claim,
            include_siblings=False,
            include_claims=False,
        )
        context = []
        # Prefer a short supported relationship, without guessing gender from names.
        for group, role in (("Children", "Parent"), ("Parents", "Child"), ("Spouses", "Spouse")):
            if model["groups"].get(group):
                relative = model["groups"][group][0]
                context.append(
                    {
                        "text": role + " of " + relative["name"],
                        "url": record_url("people", relative["id"]),
                    }
                )
                break
        narratives = self.repo.narratives(person_id)
        if narratives:
            narrative = narratives[0]
            context.append(
                {
                    "text": "Read: " + narrative["title"],
                    "url": record_url("people", person_id, narrative["id"]),
                }
            )
        if context:
            # Supported links always take priority over editorial disambiguators.
            # Full name context remains on the cached profile/name details.
            card["name_context"] = ""
            card["context"] = context[:1]
        return card

    def source(self, source_id: str) -> Record:
        source = self.repo.get(source_id, ["sources"])
        if source is None:
            raise NotFound
        subject_names: dict[str, str] = {}
        used_for = []
        for claim in self.repo.source_claims(source_id):
            subject_id = claim["subject_id"]
            if subject_id not in subject_names:
                subject_names[subject_id] = self.reference(subject_id)["name"]
            used_for.append(
                {
                    "id": claim["id"],
                    "label": subject_names[subject_id]
                    + " — "
                    + LABELS.get(claim["assertion_type"], "Historical claim"),
                }
            )
        counts = Counter(item["label"].casefold() for item in used_for)
        seen: Counter[str] = Counter()
        for item in sorted(used_for, key=lambda item: item["id"]):
            label = item["label"].casefold()
            if counts[label] > 1:
                seen[label] += 1
                item["label"] += f" (claim {seen[label]} of {counts[label]})"
        used_for.sort(key=lambda item: (item["label"].casefold(), item["id"]))
        return {
            **self.source_card(source),
            "citations": [
                self.citation(c["id"]) for c in self.repo.source_items(source_id, "citations")
            ],
            "assets": [
                {k: a[k] for k in ("id", "label", "media_type", "bytes_available")}
                for a in self.repo.source_items(source_id, "assets")
            ],
            "used_for": used_for,
        }
