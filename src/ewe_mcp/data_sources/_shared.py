"""Shared constants for data source modules."""

import re

_TAXON_PATTERN = re.compile(r"^[A-Za-z][A-Za-z\-\. ]*$")


def sanitize_taxon_name(name: str) -> str:
    """Collapse whitespace and strip leading/trailing spaces from a taxon name."""
    return re.sub(r'\s+', ' ', name).strip()


_VALID_GLOBI_INTERACTION_TYPES = frozenset({
    "preysOn", "eatenBy", "parasiteOf", "hostOf",
    "pollinates", "pollinatedBy", "symbiontOf", "kills",
})
