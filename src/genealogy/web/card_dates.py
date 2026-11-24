"""Compact dates from accepted evidence, without promoting uncertain birth claims."""

from __future__ import annotations

import re
from typing import Any


def _compact_date(d: dict[str, Any]) -> tuple[str, str] | None:
    raw, precision = d["raw"], d.get("precision", "exact")
    # Unknown/Julian/Quaker dates remain qualified in the profile.
    if d.get("calendar") not in {None, "gregorian"}:
        return None
    normalized = d.get("normalized")
    year = normalized[:4] if normalized else None
    if not year and precision == "exact":
        # Deliberately narrow full-string grammar; no fuzzy date parsing.
        match = re.fullmatch(
            r"(?:\d{1,2} (?:January|February|March|April|May|June|July|August|September|October|November|December) )?(\d{4})",
            raw,
        )
        if match:
            year = match[1]
    if not year:
        return None
    if precision == "before" and "Upper bound derived from documented existence" in d.get(
        "interpretation_note", ""
    ):
        return "recorded", year
    if precision in {"exact", "about"}:
        return precision, year
    # A normalized Dec 31 alone does not establish inclusive semantics.
    return None


def card_dates(claims: list[dict[str, Any]]) -> list[str]:
    events: dict[str, set[tuple[str, str] | None]] = {}
    for c in claims:
        typ = c["assertion_type"]
        if c["collection"] == "assertions" and typ in {"person.birth", "person.death"}:
            # Unparseable claims count too: one precise source must not silently
            # override another current source with an uncertain or different date.
            events.setdefault(typ, set()).add(_compact_date(c["value"]["date"]))
    result = []
    exact: dict[str, str] = {}
    for typ in ("person.birth", "person.death"):
        values = events.get(typ, set())
        if len(values) != 1 or None in values:
            continue
        value = next(iter(values))
        assert value is not None
        precision, year = value
        if precision == "recorded":
            result.append("Recorded in " + year)
        elif precision == "exact":
            exact[typ] = year
        else:
            result.append(("Born c. " if typ == "person.birth" else "Died c. ") + year)
    birth, death = exact.get("person.birth"), exact.get("person.death")
    if birth or death:
        result = [
            text
            for text in result
            if text not in {"Recorded in " + str(birth), "Recorded in " + str(death)}
        ]
    if birth and death:
        result.insert(0, birth + "–" + death)
    elif birth or death:
        result.insert(0, ("Born " + birth) if birth else "Died " + str(death))
    return list(dict.fromkeys(result))
