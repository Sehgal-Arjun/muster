"""Schema.org JSON-LD, rendered rather than discarded.

An Alaska Airlines confirmation carries a whole FlightReservation twice: in a
<script type="application/ld+json"> in the HTML part, and bare in the middle of
the plain-text part. The script copy was dropped with every other script; the
plain copy went in as raw JSON, unreadable and long enough to swallow a chunk.
"""

from __future__ import annotations

import json

from muster.ldjson import render, strip_bare_blocks
from muster.parser import parse_message

FLIGHT = {
    "@context": "https://schema.org",
    "@type": "FlightReservation",
    "reservationId": "MXUFEM",
    "reservationStatus": "https://schema.org/ReservationConfirmed",
    "underName": {"@type": "Person", "name": "Sam Reyes"},
    "reservationFor": {
        "@type": "Flight",
        "flightNumber": "42",
        "departureTime": "2026-07-25T13:38:00-07:00",
        "provider": {"@type": "Airline", "name": "Alaska", "iataCode": "AS"},
        "departureAirport": {"@type": "Airport", "name": "SFO", "iataCode": "SFO"},
        "arrivalAirport": {"@type": "Airport", "name": "JFK", "iataCode": "JFK"},
    },
}
BLOCK = json.dumps([FLIGHT])


def _message(body: str, mime: str = "text/plain") -> bytes:
    return (
        f"Message-ID: <flight@airline.example>\r\nDate: Mon, 01 Jun 2026 09:00:00 +0000\r\n"
        f"From: Alaska <reservation@email.airline.example>\r\nTo: A <a@b>\r\n"
        f"Subject: Your flight is booked\r\n"
        f"Content-Type: {mime}; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


# --- rendering ------------------------------------------------------------


def test_the_searchable_facts_all_survive() -> None:
    out = render(BLOCK)
    for token in ("MXUFEM", "42", "SFO", "JFK", "Alaska", "AS", "2026-07-25"):
        assert token in out, token


def test_airports_keep_which_end_they_are() -> None:
    """A bare "Name: SFO" could belong to either end of the flight."""
    out = render(BLOCK)
    assert "Departure airport: SFO" in out
    assert "Arrival airport: JFK" in out


def test_schema_enum_urls_become_their_value() -> None:
    assert "Reservation status: ReservationConfirmed" in render(BLOCK)


def test_plain_urls_and_images_are_dropped() -> None:
    out = render(json.dumps({"@context": "https://schema.org", "@type": "Thing",
                             "name": "X", "url": "https://example.com/a",
                             "image": "https://example.com/b.png"}))
    assert "example.com" not in out
    assert "Name: X" in out


def test_rendering_is_generic_across_schemas() -> None:
    """A schema nobody wrote code for still works the day it arrives."""
    lodging = json.dumps({
        "@context": "https://schema.org", "@type": "LodgingReservation",
        "reservationId": "HTL999",
        "reservationFor": {"@type": "LodgingBusiness", "name": "Hotel Zwei"},
        "checkinTime": "2026-09-01T15:00:00+02:00",
    })
    out = render(lodging)
    assert "HTL999" in out and "Hotel Zwei" in out and "2026-09-01" in out


def test_no_ldjson_renders_nothing() -> None:
    assert render("just an ordinary message body") == ""
    assert render("") == ""


def test_malformed_ldjson_is_skipped_not_fatal() -> None:
    assert render('<script type="application/ld+json">{"@context": broken</script>') == ""


# --- integration through the parser --------------------------------------


def test_bare_block_in_a_plain_part_is_rendered_and_removed() -> None:
    doc = parse_message(_message(f"Alaska Airlines\n\n{BLOCK}\n\nSafe travels."), "x")

    assert "MXUFEM" in doc.body
    assert "Departure airport: SFO" in doc.body
    assert '"@context"' not in doc.body, "raw JSON must not reach the index"
    assert "Safe travels." in doc.body, "the prose around it survives"


def test_script_block_in_an_html_part_is_rendered() -> None:
    html = (
        f'<html><head><script type="application/ld+json">{BLOCK}</script></head>'
        "<body><p>Your flight is booked.</p></body></html>"
    )
    doc = parse_message(_message(html, mime="text/html"), "x")
    assert "MXUFEM" in doc.body
    assert "Arrival airport: JFK" in doc.body


def test_structured_data_leads_the_body() -> None:
    """The confirmation code is the most likely thing to be searched for."""
    doc = parse_message(_message(f"Alaska Airlines\n\n{BLOCK}\n\nSafe travels."), "x")
    assert doc.body.startswith("Flight reservation")


def test_schema_urls_are_not_mangled_by_url_collapsing() -> None:
    """Extraction runs before URL collapsing, which would rewrite the enum URL."""
    doc = parse_message(_message(f"{BLOCK}"), "x")
    assert "ReservationConfirmed" in doc.body
    assert "Reservation status: schema.org" not in doc.body


def test_a_message_with_no_ldjson_is_unaffected() -> None:
    doc = parse_message(_message("Just a normal note about the warehouse lease."), "x")
    assert doc.body == "Just a normal note about the warehouse lease."


def test_strip_leaves_surrounding_prose() -> None:
    text = f"before\n{BLOCK}\nafter"
    stripped = strip_bare_blocks(text)
    assert "before" in stripped and "after" in stripped
    assert "@context" not in stripped


# --- the entity-encoded padding found alongside it ------------------------


def test_entity_encoded_invisibles_are_stripped() -> None:
    """Alaska's plain part pads with literal &zwnj; which nothing unescapes."""
    from muster.textclean import strip_invisibles

    assert strip_invisibles("Flight" + "&zwnj;" * 40 + "booked") == "Flightbooked"
    assert strip_invisibles("a&#8203;b&nbsp;c") == "abc"


def test_ordinary_entities_are_left_alone() -> None:
    """Unescaping a plain-text body wholesale would rewrite prose that meant it."""
    from muster.textclean import strip_invisibles

    assert strip_invisibles("Tom &amp; Jerry &lt;3") == "Tom &amp; Jerry &lt;3"
