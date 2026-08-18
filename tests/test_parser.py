from __future__ import annotations

import mailbox
from pathlib import Path

import pytest

from muster.chunking import chunk_text, estimate_tokens
from muster.parser import parse_message
from muster.quoting import clean_body, strip_quotes, strip_signature

FIXTURE = Path(__file__).parent / "fixtures" / "sample.mbox"
OWN = {"you@example.com"}


@pytest.fixture(scope="module")
def raws() -> dict[str, bytes]:
    """Fixture messages keyed by Message-ID, so tests name what they mean."""
    box = mailbox.mbox(str(FIXTURE), factory=None, create=False)
    try:
        out = {}
        for key in box.keys():
            raw = box.get_bytes(key)
            mid = box[key].get("Message-ID") or f"key-{key}"
            out[str(mid).strip("<>")] = raw
        return out
    finally:
        box.close()


def parsed(raws, mid: str):
    return parse_message(raws[mid], source_uid=mid, meta={}, own_addresses=OWN)


# --- the ordinary case ----------------------------------------------------


def test_plain_body_keeps_content_and_drops_quote(raws) -> None:
    doc = parsed(raws, "ordinary-1@example.com")
    assert "warehouse lease renewal at 4200 EUR" in doc.body
    assert "Could you confirm the renewal figure" not in doc.body
    assert "wrote:" not in doc.body


def test_signature_with_phone_is_stripped(raws) -> None:
    doc = parsed(raws, "ordinary-1@example.com")
    assert "+49 30 1234 5678" not in doc.body
    assert "https://example.com" not in doc.body


def test_headers_and_date(raws) -> None:
    doc = parsed(raws, "ordinary-1@example.com")
    assert doc.title == "Re: Q2 warehouse lease renewal"
    assert doc.doc_date.startswith("2026-03-16T09:14:00")
    assert doc.date_source == "header"
    assert doc.counterparty == "bob@example.com"
    assert doc.mime == "text/plain"


# --- German ---------------------------------------------------------------


def test_german_subject_is_rfc2047_decoded(raws) -> None:
    doc = parsed(raws, "deutsch-2@beispiel.de")
    assert doc.title == "Angebot für die Solaranlage"


def test_latin1_body_decodes_and_german_quote_is_dropped(raws) -> None:
    doc = parsed(raws, "deutsch-2@beispiel.de")
    assert "18.400 EUR" in doc.body
    assert "Solaranlage" in doc.body or "Angebot" in doc.body
    assert "Kannst du mir ein Angebot schicken" not in doc.body
    assert "schrieb" not in doc.body


def test_german_signature_with_phone_is_stripped(raws) -> None:
    doc = parsed(raws, "deutsch-2@beispiel.de")
    assert "+49 89 998877" not in doc.body
    assert "Mit freundlichen" not in doc.body


def test_german_outlook_header_block_is_dropped(raws) -> None:
    doc = parsed(raws, "outlook-de-3@beispiel.de")
    assert "XR-99120" in doc.body
    assert "alter zitierter text" not in doc.body
    assert "Gesendet:" not in doc.body


# --- HTML -----------------------------------------------------------------


def test_html_only_message_is_converted(raws) -> None:
    doc = parsed(raws, "htmlonly-4@shop.example")
    assert "88213" in doc.body
    assert "00340434161094023456" in doc.body
    assert "<p>" not in doc.body
    assert "Previous message you should not index" not in doc.body


# --- attachments ----------------------------------------------------------


def test_pdf_attachment_is_captured(raws) -> None:
    doc = parsed(raws, "attachment-5@lieferant.de")
    assert len(doc.attachments) == 1
    attachment = doc.attachments[0]
    assert attachment.filename == "Rechnung_2026-0042.pdf"
    assert attachment.mime == "application/pdf"
    assert attachment.data.startswith(b"%PDF")


def test_nameless_attachment_still_captured(raws) -> None:
    doc = parsed(raws, "noname-12@example.com")
    assert "wildebeest" in doc.body
    assert len(doc.attachments) == 1
    assert doc.attachments[0].filename is None


# --- malformed input: none of these may raise -----------------------------


def test_every_fixture_parses_without_raising(raws) -> None:
    for mid, raw in raws.items():
        doc = parse_message(raw, source_uid=mid, own_addresses=OWN)
        assert doc.content_hash
        assert doc.doc_date
        assert doc.date_source


def test_missing_date_falls_back(raws) -> None:
    doc = parsed(raws, "nodate-6@example.com")
    assert doc.date_source in {"received", "ingest", "provider"}
    assert "porcupine" in doc.body


def test_unparseable_date_is_recorded_not_fatal(raws) -> None:
    doc = parsed(raws, "baddate-7@example.com")
    assert doc.date_source != "header"
    assert any("Date header" in w for w in doc.warnings)
    assert "marmoset" in doc.body


def test_provider_internal_date_used_when_header_missing(raws) -> None:
    doc = parse_message(
        raws["nodate-6@example.com"],
        source_uid="x",
        meta={"internal_date": "1772496000000"},
    )
    assert doc.date_source == "provider"
    assert doc.doc_date.startswith("2026-03")


def test_invented_charset_still_yields_text(raws) -> None:
    doc = parsed(raws, "badcharset-8@example.com")
    assert "axolotl" in doc.body


def test_missing_mime_boundary_still_yields_text(raws) -> None:
    doc = parsed(raws, "badmime-9@example.com")
    assert "capybara" in doc.body.lower()


def test_empty_body_is_warned_not_fatal(raws) -> None:
    doc = parsed(raws, "empty-10@example.com")
    assert doc.body == ""
    assert "empty body and no attachments" in doc.warnings


def test_missing_subject_and_from(raws) -> None:
    doc = parsed(raws, "headerless-11@example.com")
    assert doc.title == "(no subject)"
    assert doc.counterparty is None


def test_truncated_base64_still_decodes_what_it_can(raws) -> None:
    doc = parsed(raws, "headerless-11@example.com")
    assert "okapi" in doc.body


def test_from_line_inside_body_survives_mbox_round_trip(raws) -> None:
    doc = parsed(raws, "fromline-13@example.com")
    assert "pangolin" in doc.body


# --- counterparty ---------------------------------------------------------


def test_outbound_message_counterparty_is_the_recipient(raws) -> None:
    doc = parsed(raws, "outbound-14@example.com")
    assert doc.counterparty == "chidi@partner.example"


def test_sent_label_marks_outbound_without_config(raws) -> None:
    doc = parse_message(
        raws["outbound-14@example.com"], source_uid="x", meta={"labels": ["SENT"]}
    )
    assert doc.counterparty == "chidi@partner.example"


# --- quoting unit tests ---------------------------------------------------


def test_signature_without_phone_or_url_is_kept() -> None:
    body, sig = strip_signature("Yes that works.\n\nThanks,\nBob")
    assert sig is None
    assert "Bob" in body


def test_signature_delimiter_with_url_is_stripped() -> None:
    body, sig = strip_signature("Yes that works.\n\n--\nBob Fischer\nhttps://example.com")
    assert sig is not None
    assert "example.com" not in body


def test_long_trailing_block_is_not_a_signature() -> None:
    tail = "\n".join(f"line {i} +49 30 111" for i in range(8))
    body, sig = strip_signature(f"Real content.\n\n--\n{tail}")
    assert sig is None
    assert "line 7" in body


def test_trailing_quote_run_is_dropped() -> None:
    assert strip_quotes("Answer here.\n\n> old\n> older") == "Answer here."


def test_clean_body_on_empty_input() -> None:
    assert clean_body("") == ""
    assert clean_body("   \n\n  ") == ""


# --- chunking -------------------------------------------------------------


def test_chunking_splits_long_text_with_overlap(raws) -> None:
    doc = parsed(raws, "long-15@example.com")
    chunks = chunk_text(doc.body)
    assert len(chunks) > 1
    assert any("quetzal" in c for c in chunks)
    assert all(estimate_tokens(c) <= 512 * 1.3 for c in chunks)

    # Consecutive chunks must share text, or a passage on the boundary is lost.
    assert any(
        set(chunks[i].split("\n\n")) & set(chunks[i + 1].split("\n\n"))
        for i in range(len(chunks) - 1)
    )


def test_short_text_is_one_chunk() -> None:
    assert chunk_text("A short note.") == ["A short note."]


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_single_oversized_paragraph_is_split() -> None:
    paragraph = "word " * 4000
    chunks = chunk_text(paragraph)
    assert len(chunks) > 1
    assert all(estimate_tokens(c) <= 512 * 1.5 for c in chunks)


def test_text_without_spaces_is_still_bounded() -> None:
    chunks = chunk_text("x" * 20000)
    assert len(chunks) > 1
