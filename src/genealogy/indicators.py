"""Independent indicators from current, audience-visible structured evidence only."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from importlib.resources import files
from typing import Any, cast

from .publication_inputs import PublicationInputs, freeze, validate_component

Record = dict[str, Any]
RULE_VERSION = "person-indicators-v6"

MEMBERSHIP: dict[str, Record] = {
    "tree": {"label": "In the family tree", "icon": "family-tree"},
    "possible": {"label": "Possible family connection", "icon": "family-possible"},
    "other": {"label": "Other person in records", "icon": None},
}


def membership(person: Record) -> Record | None:
    scope = person.get("family_scope")
    if scope not in MEMBERSHIP or "raw_name" in person:
        return None
    return {"scope": scope, **MEMBERSHIP[scope]}


def _exact_date(claim: Record) -> date | None:
    value = claim["value"]["date"]
    if value.get("precision", "exact") != "exact" or value.get("calendar") != "gregorian":
        return None
    normalized = value.get("normalized") or ""
    # Do not parse raw prose, approximate dates or non-Gregorian calendars.
    if len(normalized) != 10:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None


def _age_at_death(claims: list[Record]) -> int | None:
    dates: dict[str, set[date | None]] = {}
    for c in claims:
        kind = c["assertion_type"]
        if kind not in {"person.birth", "person.death"} or c["collection"] == "history":
            continue
        if c["collection"] != "assertions" or c["status"] != "accepted":
            return None
        dates.setdefault(kind, set()).add(_exact_date(c))
    if any(
        len(dates.get(k, set())) != 1 or None in dates[k] for k in ("person.birth", "person.death")
    ):
        return None
    born = next(iter(dates["person.birth"]))
    died = next(iter(dates["person.death"]))
    if born is None or died is None or died < born:
        return None
    return died.year - born.year - ((died.month, died.day) < (born.month, born.day))


def _current(claim: Record) -> bool:
    return claim.get("collection") in {"assertions", "alternatives"} and not claim.get("retired")


def _indicator(
    icon: str,
    label: str,
    detail: str,
    claims: list[Record],
    basis: str = "documented",
    **extra: Any,
) -> Record:
    return {
        "icon": icon,
        "label": label,
        "detail": detail,
        "basis": basis,
        "rule_version": RULE_VERSION,
        "assertion_ids": sorted(c["id"] for c in claims),
        **extra,
    }


def name_rules() -> Mapping[str, Any]:
    """Fresh immutable generic statistics only; no record-bound context or cache."""
    rules = json.loads(files("genealogy").joinpath("historical-names.json").read_text())
    validate_component("historical_name_rules", rules)
    return cast(Mapping[str, Any], freeze(rules))


def _historical_name(
    claims: list[Record],
    accepted: list[Record],
    build_year: int,
    contexts: Mapping[str, str],
    rules: Mapping[str, Any],
) -> Record | None:
    # An explicit unresolved/unknown sex claim suppresses statistical fallback.
    if any(c["assertion_type"] in {"person.sex", "person.living"} for c in claims):
        return None
    births = [c for c in claims if c["assertion_type"] == "person.birth"]
    if len(births) != 1 or births[0] not in accepted:
        return None
    birth = births[0]
    d = birth["value"]["date"]
    precision = d.get("precision", "exact")
    if (
        precision not in {"exact", "before"}
        or d.get("calendar") in {"julian", "unknown", "quaker_numeric"}
        or (precision == "before" and d.get("calendar") != "gregorian")
    ):
        return None
    try:
        born = date.fromisoformat(d.get("normalized") or "")
    except ValueError:
        return None
    if born.year >= build_year - 110 or not 1530 <= born.year <= 1919:
        return None
    names = [c for c in claims if c["assertion_type"] == "person.name"]
    if not names or any(c not in accepted for c in names):
        return None
    # Only an unambiguous first token in given-name-first research labels. No
    # nickname expansion, transliteration, surname matching, or fuzzy spelling.
    if any(not c["value"]["raw"].split() for c in names):
        return None
    # Editorial uncertainty anywhere in the name also qualifies the first token.
    if any(re.search(r"[\[?]|\bprovisional\b", c["value"]["raw"], re.IGNORECASE) for c in names):
        return None
    tokens = {c["value"]["raw"].split()[0].casefold() for c in names}
    if len(tokens) != 1 or not re.fullmatch(r"[a-z]+", next(iter(tokens))):
        return None
    contextual = [
        c for c in accepted if c["assertion_type"] in {"person.birth", "person.residence"}
    ]
    # Exact, versioned place components; no substring or abbreviation guesses.
    terms = {t.casefold() for t in rules["geography"]["US"]}
    us = any(
        component.strip().casefold() in terms
        for c in contextual
        for component in re.split(r"[,;]", c["value"].get("raw_place", ""))
    )
    context_claims = [
        c
        for c in accepted
        if any(
            contexts.get(e["citation_id"]) == "US"
            for e in c.get("evidence", [])
            if e["stance"] == "supports"
        )
    ]
    if not us and not context_claims:
        return None
    token = next(iter(tokens))
    rule = (
        rules["cohorts"].get(str(born.year // 10 * 10), {}).get(token)
        if precision == "exact" and born.year >= 1880
        else rules["stable_historical_names"].get(token)
    )
    if not rule:
        return None
    extrapolated = precision == "before" or born.year < 1880
    detail = (
        "Extrapolated name usage: this exact spelling occurs in English marriage records "
        "from each of four periods spanning 1530–1830 and has the same predominant "
        "recorded category in all four U.S. SSA decades, 1880–1919. "
        "The accepted birth bound does not establish an exact historical cohort. "
        if extrapolated
        else f"Name usage in United States Social Security applications for the {born.year // 10 * 10}s. "
    )
    label = rule["icon"].capitalize() + " — inferred from historical first-name usage"
    return _indicator(
        rule["icon"],
        label,
        detail + "A display inference, not a sourced sex or gender identity. "
        f"Conservative SSA recorded-name share: {rule['minimum_share']:.1%}; "
        "a later dataset statistic, not a probability about this person. Incomplete historical coverage.",
        list({c["id"]: c for c in names + births + contextual + context_claims}.values()),
        "inferred",
        inference_version=rules["version"],
        confidence=rule["minimum_share"],
        extrapolated=extrapolated,
        confidence_basis="SSA recorded-name share; not individual or earlier-period probability",
    )


def attributes(
    person: Record,
    own_claims: list[Record],
    *,
    build_year: int | None = None,
    eligible_ids: set[str] | None = None,
    contexts: Mapping[str, str] | None = None,
    rules: Mapping[str, Any] | None = None,
) -> list[Record]:
    """One semantic layer; called after audience closure, never an authorization rule.

    Compiler supplies verified evidence/dependency IDs. No build year means no
    time-dependent inference (compatibility with previously imported projections).
    """
    claims = [
        c
        for c in own_claims
        if _current(c) and (not person.get("id") or c["subject_id"] == person["id"])
    ]
    supported = [c for c in claims if eligible_ids is None or c["id"] in eligible_ids]
    accepted = [
        c for c in supported if c["collection"] == "assertions" and c["status"] == "accepted"
    ]
    result: list[Record] = []
    sexes = [c for c in claims if c["assertion_type"] == "person.sex"]
    values = {c["value"].get("sex") for c in sexes}
    if (
        sexes
        and all(c in accepted for c in sexes)
        and len(values) == 1
        and values <= {"male", "female"}
    ):
        sex = next(iter(values))
        result.append(
            _indicator(
                sex,
                sex.capitalize() + " — documented",
                "Recorded sex in the cited evidence; not inferred from a name.",
                sexes,
            )
        )
    elif not sexes and build_year is not None:
        inferred = _historical_name(
            claims,
            accepted,
            build_year,
            contexts or {},
            rules if rules is not None else name_rules(),
        )
        if inferred:
            result.append(inferred)
    deaths = [
        c
        for c in supported
        if c["assertion_type"] == "person.death"
        and (c["collection"], c["status"])
        in {("assertions", "accepted"), ("alternatives", "conflicting")}
    ]
    living_claims = [c for c in claims if c["assertion_type"] == "person.living"]
    living = []
    if build_year is not None and living_claims and all(c in accepted for c in living_claims):
        for c in living_claims:
            try:
                as_of = date.fromisoformat(c["value"]["as_of"])
                through = date.fromisoformat(c["value"]["valid_through"])
            except (ValueError, KeyError):
                continue
            if (
                as_of.year <= build_year
                and date(build_year, 12, 31) <= through
                and as_of <= through
            ):
                living.append(c)
    if deaths:
        result.append(
            _indicator(
                "deceased",
                "Deceased",
                "Death is documented; individual dates may remain disputed.",
                deaths,
            )
        )
    elif living and not any(c["assertion_type"] == "person.death" for c in claims):
        result.append(
            _indicator(
                "living",
                "Living",
                "Reported living as of "
                + max(c["value"]["as_of"] for c in living)
                + "; revalidate by "
                + min(c["value"]["valid_through"] for c in living)
                + ".",
                living,
            )
        )
    elif (
        build_year is not None
        and not living_claims
        and not any(c["assertion_type"] == "person.death" for c in claims)
    ):
        births = [c for c in claims if c["assertion_type"] == "person.birth"]
        bounds = []
        for c in births:
            d = c["value"]["date"]
            if (
                c not in accepted
                or d.get("precision", "exact") not in {"exact", "before"}
                or d.get("calendar") != "gregorian"
            ):
                break
            try:
                bound = date.fromisoformat(d.get("normalized") or "")
            except ValueError:
                break
            if bound.year >= build_year - 110:
                break
            bounds.append(bound)
        if births and len(bounds) == len(births) and len(set(bounds)) == 1:
            result.append(
                _indicator(
                    "presumed-deceased",
                    "Presumed deceased",
                    f"Accepted historical birth bound is before {build_year - 110}; no direct death record. "
                    "This inference supplies neither a death date nor publication permission.",
                    births,
                    "inferred",
                )
            )
    accepted_deaths = [c for c in deaths if c in accepted]
    if accepted_deaths:
        stages = {c["value"].get("life_stage") for c in accepted_deaths} - {None, "unknown"}
        age = _age_at_death(claims)
        explicit_child = bool(stages) and stages <= {"infant", "child"}
        adult_stage = any(c["value"].get("life_stage") == "adult" for c in deaths)
        # The age path must not consume a visible but unsupported birth.
        age_supported = all(c in accepted for c in claims if c["assertion_type"] == "person.birth")
        if not adult_stage and (
            (explicit_child and (age is None or age < 18))
            or (age_supported and age is not None and age < 18)
        ):
            label = (
                "Died in infancy"
                if stages == {"infant"} or (not stages and age == 0)
                else "Died as a child"
            )
            result.append(
                _indicator(
                    "child",
                    label,
                    "Childhood at death, not present living status.",
                    accepted_deaths
                    + (
                        [c for c in accepted if c["assertion_type"] == "person.birth"]
                        if age_supported and age is not None
                        else []
                    ),
                )
            )
    elif any(i["icon"] == "living" for i in result) and build_year is not None:
        births = [c for c in claims if c["assertion_type"] == "person.birth"]
        dates = {_exact_date(c) for c in births}
        if births and all(c in accepted for c in births) and len(dates) == 1 and None not in dates:
            born = next(iter(dates))
            if born is not None and born <= date(build_year, 1, 1) and build_year - born.year < 18:
                result.append(
                    _indicator(
                        "child",
                        "Child",
                        f"Documented living child; under 18 throughout {build_year}.",
                        births + living,
                    )
                )
    if person.get("slave") is True:
        result.append(
            _indicator(
                "slave",
                "Slave",
                "Supported enslavement record; independent of family membership.",
                [],
            )
        )
    return result


def evidence_eligibility(document: Record, records: list[Record]) -> dict[str, bool]:
    """Audit the same audience-local evidence gate used for indicator derivation.

    The returned keys are published assertion IDs only. Canonical dependency
    details remain private; callers must not attach them to serving projections.
    """
    published = {
        r["id"]: {**r, "collection": collection}
        for collection, rows in document.items()
        if isinstance(rows, list)
        for r in rows
    }
    original = {r["id"]: r for r in records}
    cache: dict[str, bool] = {}

    def verified(rid: str, visiting: frozenset[str] = frozenset()) -> bool:
        if rid in cache:
            return cache[rid]
        c = published.get(rid)
        if not c or rid in visiting or c.get("collection") not in {"assertions", "alternatives"}:
            return False
        evidence = c.get("evidence", [])
        qualifying = [
            e
            for e in evidence
            if e["stance"] == "supports"
            and (cit := published.get(e["citation_id"], {}))
            and (
                published.get(cit.get("source_id"), {}).get("source_kind")
                in {
                    "original_record",
                    "record_image",
                    "published_transcription",
                    "newspaper",
                    "index",
                    "compiled_genealogy",
                    "research_report",
                    "oral_or_family_testimony",
                }
                and cit.get("inspection")
                in {
                    "original_image_inspected",
                    "published_image_inspected",
                    "text_or_transcription_inspected",
                    "index_only",
                    "reported_by_family",
                }
            )
        ]
        supported = bool(qualifying)
        # Contradictions must not support a positive fact or name/birth inference.
        # Competing death dates can still establish that a death occurred.
        if c["assertion_type"] in {"person.sex", "person.living", "person.name", "person.birth"}:
            supported = supported and not any(e["stance"] == "contradicts" for e in evidence)
        # Direct sex/living claims require explicit qualifying evidence.
        if c["assertion_type"] in {"person.sex", "person.living"}:
            supported = supported and any(e["explicitness"] == "explicit" for e in qualifying)
        deps = original[rid].get("dependencies", [])
        cache[rid] = supported and all(
            published.get(dep, {}).get("status") == "accepted" and verified(dep, visiting | {rid})
            for dep in deps
        )
        return cache[rid]

    return {rid: verified(rid) for rid, c in published.items() if c.get("assertion_type")}


def annotate_indicators(
    document: Record, records: list[Record], *, inputs: PublicationInputs
) -> None:
    """Qualify evidence transitively without copying private dependency metadata."""
    eligible = {
        rid for rid, supported in evidence_eligibility(document, records).items() if supported
    }
    published = {
        r["id"]: {**r, "collection": collection}
        for collection, rows in document.items()
        if isinstance(rows, list)
        for r in rows
    }
    # Reviewed record context is not a birthplace or sex claim. A source may
    # discuss many people; newly reviewed contexts can be scoped to exact people.
    reviewed_contexts = inputs.reviewed_name_contexts
    by_subject: dict[str, list[Record]] = {}
    for c in published.values():
        if c.get("assertion_type"):
            by_subject.setdefault(c["subject_id"], []).append(c)
    for person in document["people"]:
        person["indicators"] = attributes(
            person,
            by_subject.get(person["id"], []),
            build_year=inputs.build_year,
            rules=inputs.historical_name_rules,
            eligible_ids=eligible,
            contexts={
                rid: value["region"]
                for rid, value in reviewed_contexts.items()
                if "subject_ids" not in value or person["id"] in value["subject_ids"]
            },
        )
