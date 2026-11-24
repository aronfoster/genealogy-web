"""Planner statistics refreshed before serving an activated generation."""

STATISTICS_SQL = "ANALYZE genealogy.serving_entity; ANALYZE genealogy.serving_subject; ANALYZE genealogy.active_generation;"
