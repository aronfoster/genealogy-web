"""Source catalog labels; validation values come from the packaged corpus schema."""

import json
from importlib.resources import files

SOURCE_KINDS = frozenset(
    next(
        item["properties"]["source_kind"]["enum"]
        for item in json.loads(
            files("genealogy").joinpath("schemas/entity.schema.json").read_text()
        )["oneOf"]
        if "source_kind" in item.get("properties", {})
    )
)

SOURCE_KIND_LABELS = {
    "original_record": "Original records",
    "record_image": "Record images",
    "published_transcription": "Published transcriptions",
    "index": "Indexes",
    "newspaper": "Newspapers",
    "compiled_genealogy": "Published family histories",
    "oral_or_family_testimony": "Family testimony",
    "research_report": "Research notes",
    "website": "Websites",
}
