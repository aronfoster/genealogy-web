"""Separate missing routes from broken serving data."""


class NotFound(Exception):
    """The requested resource is absent from this audience."""


class ProjectionInvariantError(Exception):
    """A serving document violates the compiler's closed-graph contract."""
