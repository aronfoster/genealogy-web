"""Name selection over current, audience-visible assertions only."""

from typing import Any

NAME_LABELS = {
    "birth": "Birth / maiden name",
    "married": "Married name",
    "familiar": "Known as",
    "other": "Other name",
    "unknown": "Recorded name",
}


def select_name(names: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer an explicit selection; retain a stable fallback for uncurated names."""
    preferred = [n for n in names if n["value"].get("preferred_display")]
    if len(preferred) > 1:
        raise ValueError("multiple current preferred names")
    return next(iter(preferred), None) or max(
        names,
        key=lambda n: (len(n["value"]["raw"]), n["value"]["raw"].casefold(), n["id"]),
        default=None,
    )
