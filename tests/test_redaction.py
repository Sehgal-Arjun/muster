"""One-time credentials must not reach the index."""

from __future__ import annotations

import json
import sqlite3

import pytest

from muster.blobs import BlobStore
from muster.config import RedactionConfig, RedactionRule
from muster.ingest import ingest_message
from muster.parser import parse_message
from muster.redaction import Redactor
from muster.search import keyword_search


def _raw(sender: str, subject: str, body: str) -> bytes:
    return (
        f"Message-ID: <m-{abs(hash(subject))}@example.com>\r\n"
        f"Date: Mon, 16 Mar 2026 09:00:00 +0000\r\n"
        f"From: {sender}\r\nTo: Sam <you@example.com>\r\n"
        f"Subject: {subject}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


OTP = _raw(
    "Spotify <no-reply@spotify.com>",
    "187431 - your Spotify login code",
    "Your login code is 187431. It expires in ten minutes.",
)
MAGIC = _raw(
    "Claude <noreply@anthropic.com>",
    "Your secure link to Claude.ai is here",
    "Sign in with this link: https://claude.ai/magic/AbC123dEf456GhI789",
)
NEWSLETTER = _raw(
    "Shop <no-reply@shop.example>",
    "Half price warehouse clearance",
    "Our warehouse clearance sale starts today with everything half price.",
)
REAL = _raw(
    "Bob <bob@example.com>",
    "Your password policy question",
    "About the password policy you asked for: rotation is every 90 days for the warehouse team.",
)


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(RedactionConfig())


# --- matching -------------------------------------------------------------


def test_otp_from_a_noreply_sender_is_matched(redactor) -> None:
    doc = parse_message(OTP, "otp", redactor=redactor)
    assert doc.redacted_by == "one-time-credentials"


def test_magic_link_is_matched(redactor) -> None:
    doc = parse_message(MAGIC, "magic", redactor=redactor)
    assert doc.redacted_by == "one-time-credentials"


def test_a_noreply_newsletter_is_not_matched(redactor) -> None:
    """The sender pattern alone is far too broad to act on."""
    assert parse_message(NEWSLETTER, "n", redactor=redactor).redacted_by is None


def test_a_real_person_writing_about_passwords_is_not_matched(redactor) -> None:
    """And so is the subject pattern alone."""
    doc = parse_message(REAL, "r", redactor=redactor)
    assert doc.redacted_by is None
    assert "rotation is every 90 days" in doc.body


# --- what redaction removes -----------------------------------------------


def test_the_code_is_gone_from_body_and_title(redactor) -> None:
    doc = parse_message(OTP, "otp", redactor=redactor)
    assert doc.body == ""
    assert "187431" not in doc.title
    assert "Spotify" in doc.title, "the subject stays recognisable"


def test_the_magic_link_token_is_gone(redactor) -> None:
    doc = parse_message(MAGIC, "magic", redactor=redactor)
    assert "AbC123dEf456GhI789" not in doc.body + doc.title


def test_a_redacted_message_gets_no_chunks_at_all(conn, cfg, redactor) -> None:
    """Not even the title fallback — the code is usually in the subject."""
    store = BlobStore(cfg.blob_dir)
    result = ingest_message(
        conn, store, source="gmail", source_uid="otp1", raw=OTP, redactor=redactor
    )
    assert result.created
    assert result.chunks == 0
    assert result.redacted_by == "one-time-credentials"
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


def test_a_redacted_message_is_unfindable(conn, cfg, redactor) -> None:
    store = BlobStore(cfg.blob_dir)
    ingest_message(conn, store, source="gmail", source_uid="otp1", raw=OTP, redactor=redactor)
    for query in ("187431", "Spotify login code", "login code"):
        assert keyword_search(conn, query, min_score=0.0) == []


def test_the_decision_is_recorded_for_audit(conn, cfg, redactor) -> None:
    store = BlobStore(cfg.blob_dir)
    ingest_message(conn, store, source="gmail", source_uid="otp1", raw=OTP, redactor=redactor)
    meta = json.loads(conn.execute("SELECT meta_json FROM docs").fetchone()[0])
    assert meta["redacted_by"] == "one-time-credentials"


def test_redact_mode_keeps_the_original(conn, cfg, redactor) -> None:
    """The archive stays complete and re-parseable; the index does not carry it."""
    store = BlobStore(cfg.blob_dir)
    ingest_message(conn, store, source="gmail", source_uid="otp1", raw=OTP, redactor=redactor)
    blob_path = conn.execute("SELECT blob_path FROM docs").fetchone()[0]
    assert store.get_blob(blob_path) == OTP


# --- drop mode ------------------------------------------------------------


def test_drop_mode_stores_nothing_at_all(conn, cfg) -> None:
    store = BlobStore(cfg.blob_dir)
    dropper = Redactor(RedactionConfig(mode="drop"))
    result = ingest_message(
        conn, store, source="gmail", source_uid="otp1", raw=OTP, redactor=dropper
    )

    assert result.dropped and result.doc_id is None
    assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 0
    assert list(store.iter_digests()) == [], "not even the raw original"


# --- configuration --------------------------------------------------------


def test_rules_come_from_config_not_the_code(conn, cfg) -> None:
    custom = Redactor(
        RedactionConfig(
            rules=(
                RedactionRule(name="payslips", sender="(?i)payroll", subject="(?i)payslip"),
            )
        )
    )
    payslip = _raw("Payroll <payroll@work.example>", "Your March payslip", "Net pay attached.")
    assert parse_message(payslip, "p", redactor=custom).redacted_by == "payslips"
    # The built-in rule is gone because the config replaced the list.
    assert parse_message(OTP, "o", redactor=custom).redacted_by is None


def test_redaction_can_be_disabled(redactor) -> None:
    off = Redactor(RedactionConfig(enabled=False))
    assert parse_message(OTP, "o", redactor=off).redacted_by is None


def test_an_invalid_pattern_does_not_disable_the_others(caplog) -> None:
    broken = Redactor(
        RedactionConfig(
            rules=(
                RedactionRule(name="broken", sender="(unclosed", subject=".*"),
                RedactionRule(
                    name="good", sender="(?i)no-?reply", subject="(?i)login"
                ),
            )
        )
    )
    assert parse_message(OTP, "o", redactor=broken).redacted_by == "good"


def test_no_redactor_means_no_redaction() -> None:
    assert parse_message(OTP, "o").redacted_by is None
    assert "187431" in parse_message(OTP, "o").body


def test_invalid_mode_is_rejected_at_config_load(home) -> None:
    from muster.config import load_config

    (home / "config.toml").write_text('[redaction]\nmode = "shred"\n')
    with pytest.raises(ValueError, match="redaction.mode"):
        load_config()


def test_rules_are_readable_from_toml(home) -> None:
    from muster.config import load_config

    (home / "config.toml").write_text(
        '[redaction]\nmode = "drop"\n\n'
        '[[redaction.rules]]\nname = "otp"\nsender = "noreply"\nsubject = "code"\n'
    )
    cfg = load_config()
    assert cfg.redaction.mode == "drop"
    assert [r.name for r in cfg.redaction.rules] == ["otp"]
