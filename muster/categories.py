"""Gmail category labels.

Roughly two thirds of Gmail volume is marketing, and none of it is what this
index is for. Categories are the provider's own classification of that, already
computed and reliable, so they are worth keeping and filtering on.

A message can technically carry more than one CATEGORY_* label, so a single
column stores the most exclusion-worthy one: if something is both promotional
and an update, it is promotional.
"""

from __future__ import annotations

# Ordered by how strongly the label argues the message is not correspondence.
CATEGORY_PRIORITY = ("promotions", "social", "forums", "updates", "personal")

CATEGORIES = frozenset(CATEGORY_PRIORITY)

# Excluded from retrieval unless the caller opts back in. Updates stays in:
# receipts, shipping notices and account changes land there, and those are
# exactly the things people search an archive for.
DEFAULT_EXCLUDED = frozenset({"promotions", "social"})


def normalise(name: str) -> str:
    """Accept 'promotions', 'PROMOTIONS' or 'CATEGORY_PROMOTIONS'."""
    name = (name or "").strip().lower()
    if name.startswith("category_"):
        name = name[len("category_") :]
    return name


def parse_selection(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated CLI value into known category names."""
    if not raw:
        return frozenset()
    selected = {normalise(part) for part in raw.split(",") if part.strip()}
    unknown = selected - CATEGORIES
    if unknown:
        raise ValueError(
            f"unknown categories: {', '.join(sorted(unknown))}"
            f" (known: {', '.join(CATEGORY_PRIORITY)})"
        )
    return frozenset(selected)


def category_of(labels) -> str | None:
    """The single category for a message, or None if it has no CATEGORY_* label."""
    present = {normalise(label) for label in (labels or []) if str(label).upper().startswith("CATEGORY_")}
    for candidate in CATEGORY_PRIORITY:
        if candidate in present:
            return candidate
    return None


def is_excluded(labels, excluded: frozenset[str]) -> bool:
    category = category_of(labels)
    return category is not None and category in excluded
