"""Shared constants for data source modules."""

import re

_TAXON_PATTERN = re.compile(r"^[A-Za-z][A-Za-z\-\. ]*$")
_VALID_GLOBI_INTERACTION_TYPES = frozenset({
    "preysOn", "eatenBy", "parasiteOf", "hostOf",
    "pollinates", "pollinatedBy", "symbiontOf", "kills",
})
