"""Reading dates out of a query and turning them into a filter.

"autopay tenant protection september 2024" was matching "september" and "2024"
as *words*. That is close to useless: "september" appears in every message sent
that month and in plenty sent about it, and "2024" appears in invoice numbers,
addresses and footers. Meanwhile the one thing the phrase actually tells us —
which slice of the archive to look in — was being thrown away.

So a temporal expression is removed from the text query and applied as a
``doc_date`` range instead. The remaining words do the ranking; the date does
the narrowing.

A month needs a qualifier or a year to count as a date. "early august" and
"september 2024" are unambiguous; a bare "september" is more often part of what
the message says than a statement about when it was sent, and reading it as a
filter removes the very document being looked for.

Where a month is taken as a date but the year is not given, it resolves
backwards: "early august" in October means this year's August, in March last
year's. People asking an archive about a month mean one that has happened.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

MONTHS = {
    name.lower(): number
    for number, name in enumerate(calendar.month_name)
    if name
} | {
    name.lower(): number
    for number, name in enumerate(calendar.month_abbr)
    if name
} | {"sept": 9}

# Northern-hemisphere seasons, which is where the archive is.
SEASONS = {
    "spring": (3, 5),
    "summer": (6, 8),
    "autumn": (9, 11),
    "fall": (9, 11),
    "winter": (12, 2),
}

_PART_OF_MONTH = {"early": (1, 10), "mid": (10, 20), "late": (20, 31)}

_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
_SEASON_ALT = "|".join(SEASONS)

# Ordered: the most specific pattern has to win, or "september 2024" would be
# consumed by the bare-year rule.
_PATTERNS: list[tuple[str, str]] = [
    ("part_month_year", rf"\b(?P<part>early|mid|late)\s+(?P<month>{_MONTH_ALT})\s+(?P<year>\d{{4}})\b"),
    ("part_month", rf"\b(?P<part>early|mid|late)\s+(?P<month>{_MONTH_ALT})\b"),
    ("month_year", rf"\b(?P<month>{_MONTH_ALT})\s+(?P<year>\d{{4}})\b"),
    ("season_year", rf"\b(?P<season>{_SEASON_ALT})\s+(?P<year>\d{{4}})\b"),
    ("last_season", rf"\blast\s+(?P<season>{_SEASON_ALT})\b"),
    ("this_season", rf"\bthis\s+(?P<season>{_SEASON_ALT})\b"),
    ("last_month", r"\blast\s+month\b"),
    ("this_month", r"\bthis\s+month\b"),
    ("last_year", r"\blast\s+year\b"),
    ("this_year", r"\bthis\s+year\b"),
    # A bare year, but not one glued to other digits (invoice 20241234).
    ("year", r"(?<![\d-])(?P<year>19\d{2}|20\d{2})(?![\d-])"),
]

# Deliberately absent: a bare month or season on its own. "voiceflow september"
# usually means an offer that mentions September, not mail sent in September,
# and reading it as a date filters the answer out entirely. Measured on the
# query set it changed nothing either way, so the failure mode decides it. A
# qualifier ("early august") or a year ("september 2024") is explicit enough.
_COMPILED = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _PATTERNS]


@dataclass(frozen=True)
class TemporalRange:
    since: str
    until: str
    matched: str
    kind: str


def _span(start: date, end: date, matched: str, kind: str) -> TemporalRange:
    """Inclusive day range, as the ISO8601 bounds doc_date is compared against."""
    return TemporalRange(
        since=f"{start.isoformat()}T00:00:00+00:00",
        until=f"{end.isoformat()}T23:59:59+00:00",
        matched=matched,
        kind=kind,
    )


def _month_span(year: int, month: int, matched: str, kind: str) -> TemporalRange:
    last = calendar.monthrange(year, month)[1]
    return _span(date(year, month, 1), date(year, month, last), matched, kind)


def _resolve_bare_month(month: int, today: date) -> int:
    """The most recent occurrence, including the current month."""
    return today.year if month <= today.month else today.year - 1


def _season_span(season: str, year: int, matched: str, kind: str) -> TemporalRange:
    first, last = SEASONS[season]
    if first > last:  # winter straddles the year boundary
        start = date(year - 1, first, 1)
        end = date(year, last, calendar.monthrange(year, last)[1])
    else:
        start = date(year, first, 1)
        end = date(year, last, calendar.monthrange(year, last)[1])
    return _span(start, end, matched, kind)


def _build(name: str, match: re.Match[str], today: date) -> TemporalRange | None:
    groups = match.groupdict()
    text = match.group(0)

    if name in ("month_year", "part_month_year", "part_month", "month"):
        month = MONTHS[groups["month"].lower()]
        year = int(groups["year"]) if groups.get("year") else _resolve_bare_month(month, today)
        if name.startswith("part"):
            first, last = _PART_OF_MONTH[groups["part"].lower()]
            month_end = calendar.monthrange(year, month)[1]
            return _span(
                date(year, month, min(first, month_end)),
                date(year, month, min(last, month_end)),
                text, name,
            )
        return _month_span(year, month, text, name)

    if name in ("season_year", "season", "last_season", "this_season"):
        season = groups["season"].lower()
        if groups.get("year"):
            year = int(groups["year"])
        elif name == "last_season":
            year = today.year - 1
        else:
            year = today.year
        return _season_span(season, year, text, name)

    if name == "last_month":
        year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        return _month_span(year, month, text, name)
    if name == "this_month":
        return _month_span(today.year, today.month, text, name)
    if name == "last_year":
        return _span(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), text, name)
    if name == "this_year":
        return _span(date(today.year, 1, 1), date(today.year, 12, 31), text, name)
    if name == "year":
        year = int(groups["year"])
        return _span(date(year, 1, 1), date(year, 12, 31), text, name)
    return None


def extract(query: str, *, today: date | None = None) -> tuple[str, TemporalRange | None]:
    """Split a query into (text without the date, the range it named).

    Only the first expression is used. Two dates in one query most often means
    a span nobody has asked for yet, and guessing wrong would silently hide
    results.
    """
    if not query:
        return query, None
    today = today or datetime.now(UTC).date()

    for name, pattern in _COMPILED:
        match = pattern.search(query)
        if not match:
            continue
        try:
            span = _build(name, match, today)
        except ValueError:
            continue  # e.g. February 31st
        if span is None:
            continue
        remainder = (query[: match.start()] + " " + query[match.end() :]).strip()
        remainder = re.sub(r"\s{2,}", " ", remainder)
        return remainder, span
    return query, None


def apply(
    query: str,
    since: str | None,
    until: str | None,
    *,
    today: date | None = None,
) -> tuple[str, str | None, str | None, TemporalRange | None]:
    """Fold a query's own date into the filters.

    If the caller supplied *either* bound the inferred range is dropped whole,
    rather than filling in the missing half. Combining an explicit --since with
    an until read out of the query produces a window the user never asked for
    and, when the two point opposite ways, an empty one.

    The words come out of the query either way: they are noise to BM25 whether
    or not they end up narrowing anything.
    """
    remainder, span = extract(query, today=today)
    if span is None:
        return query, since, until, None
    # A query that is nothing but a date still needs something to rank on.
    text = remainder if remainder else query
    if since is not None or until is not None:
        return text, since, until, None
    return text, span.since, span.until, span
