"""Open items: a set difference, not a summary.

The numbers here must be computed. Every test that matters is either about the
arithmetic being right or about the report admitting what it does not know.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from muster import extraction, gather as G, openitems as OI, processes


def _mail(conn, *, uid, title, when, sender, thread="T1"):
    cursor = conn.execute(
        "INSERT INTO docs (source, source_uid, content_hash, mime, doc_date,"
        " date_source, ingested_at, title, sender, counterparty, thread_uid)"
        " VALUES ('gmail', ?, ?, 'message/rfc822', ?, 'header', '2026-01-01', ?, ?, ?, ?)",
        (uid, f"h-{uid}", when, title, sender, sender, thread),
    )
    doc_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO chunks (doc_id, ord, text, text_hash) VALUES (?, 0, ?, ?)",
        (doc_id, title, f"t-{doc_id}"),
    )
    conn.commit()
    return doc_id


def _extract(conn, doc_id, **payload):
    body = {
        "date": None, "direction": None, "counterparty": None,
        "requests_of_me": [], "things_i_sent": [], "things_i_promised": [],
        "deadlines": [],
    }
    body.update(payload)
    extraction.store(conn, extraction.Extraction(
        doc_id=doc_id, payload=extraction.validate(body), model_id="test-model"
    ))


OWN = "me@example.com"
THEM = "clerk@sponsor.example"


@pytest.fixture
def process(conn):
    made = processes.create(conn, "Visa")
    processes.add_anchor(conn, made.id, "domain", "sponsor.example")
    # Own address must be derivable, since direction is computed from it.
    sent = _mail(conn, uid="sent-marker", title="hello", when="2026-01-01T00:00:00+00:00",
                 sender=OWN)
    conn.execute(
        "UPDATE docs SET meta_json = ? WHERE id = ?",
        (json.dumps({"labels": ["SENT"]}), sent),
    )
    for _ in range(3):
        pass
    # Three SENT messages so the derivation threshold is met.
    for i in range(3):
        extra = _mail(conn, uid=f"sm{i}", title="x", when="2026-01-02T00:00:00+00:00", sender=OWN)
        conn.execute("UPDATE docs SET meta_json = ? WHERE id = ?",
                     (json.dumps({"labels": ["SENT"]}), extra))
    conn.commit()
    return processes.get(conn, made.slug)


def _report(conn, process, today=date(2026, 5, 1)):
    return OI.compute(conn, G.gather(conn, process), model="test-model",
                      today=today, ask=lambda _p, _m: '{"satisfied": false}')


# --- the arithmetic --------------------------------------------------------


def test_a_request_with_nothing_matching_it_is_open(conn, process) -> None:
    asked = _mail(conn, uid="ask", title="Please send insurance",
                  when="2026-04-01T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "proof of insurance", "due": None}])
    report = _report(conn, process)
    assert report.requests_total == 1
    assert report.requests_satisfied == 0
    assert [i.kind for i in report.items] == [OI.UNSATISFIED]


def test_a_request_with_a_matching_fulfilment_is_closed(conn, process) -> None:
    asked = _mail(conn, uid="ask", title="Please send insurance",
                  when="2026-04-01T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "proof of insurance", "due": None}])
    replied = _mail(conn, uid="reply", title="Re: insurance",
                    when="2026-04-02T09:00:00+00:00", sender=OWN)
    _extract(conn, replied, direction="outbound",
             things_i_sent=[{"what": "proof of insurance certificate", "doc_ref": None}])

    report = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": true}')
    assert report.requests_satisfied == 1
    assert [i for i in report.items if i.kind == OI.UNSATISFIED] == []


def test_a_fulfilment_before_the_request_does_not_count(conn, process) -> None:
    """A receipt from last year does not answer a question asked this morning —
    which is why the set is ordered chronologically."""
    replied = _mail(conn, uid="early", title="here it is",
                    when="2026-03-01T09:00:00+00:00", sender=OWN)
    _extract(conn, replied, direction="outbound",
             things_i_sent=[{"what": "proof of insurance", "doc_ref": None}])
    asked = _mail(conn, uid="ask", title="Please send insurance",
                  when="2026-04-01T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "proof of insurance", "due": None}])

    report = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": true}')
    assert report.requests_satisfied == 0


def test_a_request_in_a_message_i_sent_is_not_owed_by_me(conn, process) -> None:
    """The error found on real data: on a fifth of outbound documents the model
    put my own asks into requests_of_me, which would read as "you owe" backwards."""
    mine = _mail(conn, uid="mine", title="Could you confirm my status?",
                 when="2026-04-01T09:00:00+00:00", sender=OWN)
    _extract(conn, mine, direction="outbound",
             requests_of_me=[{"what": "confirmation of my visa status", "due": None}])
    report = _report(conn, process)
    assert report.requests_total == 0
    assert report.items == []


def test_the_three_kinds_stay_distinct(conn, process) -> None:
    asked = _mail(conn, uid="ask", title="send it", when="2026-04-01T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "the form", "due": None}],
             deadlines=[{"what": "submit the form", "when": "2026-05-10"}])
    promised = _mail(conn, uid="mine", title="I will send it",
                     when="2026-04-02T09:00:00+00:00", sender=OWN)
    _extract(conn, promised, direction="outbound",
             things_i_promised=[{"what": "send the form next week", "when": None}])

    kinds = {i.kind for i in _report(conn, process).items}
    assert kinds == {OI.UNSATISFIED, OI.DEADLINE_NEAR, OI.UNANSWERED}


def test_every_item_cites_its_documents(conn, process) -> None:
    asked = _mail(conn, uid="ask", title="send it", when="2026-04-01T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "the form", "due": None}])
    for item in _report(conn, process).items:
        assert item.doc_ids, "an open item with no source is an assertion"


def test_the_same_request_twice_is_one_obligation(conn, process) -> None:
    """A checklist restated in a reminder is not two things to do."""
    for i, when in enumerate(("2026-04-01T09:00:00+00:00", "2026-04-08T09:00:00+00:00")):
        doc = _mail(conn, uid=f"ask{i}", title="Reminder", when=when, sender=THEM)
        _extract(conn, doc, direction="inbound",
                 requests_of_me=[{"what": "a copy of your passport", "due": None}])
    report = _report(conn, process)
    unsatisfied = [i for i in report.items if i.kind == OI.UNSATISFIED]
    assert len(unsatisfied) == 1
    assert len(unsatisfied[0].doc_ids) == 2, "both sources should be cited"
    assert report.duplicates_collapsed == 1


def test_items_with_no_possible_fulfilment_are_counted_separately(conn, process) -> None:
    """"Nothing in the archive either way" is a different claim from "checked and
    not found" — on real data 53 of 66 open items were the former."""
    asked = _mail(conn, uid="ask", title="Please complete the online portal form",
                  when="2026-04-01T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "the online portal declaration", "due": None}])
    report = _report(conn, process)
    assert report.open_without_candidates == 1
    assert "no candidate fulfilment" in report.items[0].detail


def test_a_stale_request_is_not_a_live_obligation(conn, process) -> None:
    asked = _mail(conn, uid="old", title="send it", when="2023-01-01T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "an ancient form", "due": None}])
    assert _report(conn, process, today=date(2026, 5, 1)).items == []


def test_unextracted_documents_are_reported_not_hidden(conn, process) -> None:
    """Silent omission is the dangerous failure."""
    _mail(conn, uid="unread", title="Please send insurance",
          when="2026-04-01T09:00:00+00:00", sender=THEM)
    report = _report(conn, process)
    assert report.documents_unaccounted, "an unextracted document vanished from the count"


def test_a_document_nobody_read_is_not_reported_as_unreadable(conn, process) -> None:
    """The panel said "194 documents could not be read" about 194 documents nothing
    had tried to read yet. Two different problems, so two different counts."""
    _mail(conn, uid="unread", title="Please send insurance",
          when="2026-04-01T09:00:00+00:00", sender=THEM)
    report = _report(conn, process)
    counts = report.as_json()["counts"]
    assert counts["documents_unexamined"] >= 1
    assert counts["documents_unread"] == 0


def test_a_recorded_failure_is_reported_as_unreadable(conn, process) -> None:
    doc_id = _mail(conn, uid="broken", title="attachment only",
                   when="2026-04-01T09:00:00+00:00", sender=THEM)
    extraction.record_failure(conn, doc_id, "no text", attempts=1)
    conn.commit()
    report = _report(conn, process)
    counts = report.as_json()["counts"]
    assert counts["documents_unread"] == 1
    assert doc_id not in report.documents_unexamined
    # Still accounted for, in the total that means "contributing nothing above".
    assert doc_id in report.documents_unaccounted


def test_the_report_is_deterministic(conn, process) -> None:
    asked = _mail(conn, uid="ask", title="send it", when="2026-04-01T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "the form", "due": None}])
    first = _report(conn, process).as_json()
    second = _report(conn, process).as_json()
    assert first == second


# --- dates and the prefilter -----------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-05-10", date(2026, 5, 10)),
        # A stored document timestamp. With a trailing \b this matched nothing,
        # so every request looked like it arrived today.
        ("2026-04-01T09:00:00+00:00", date(2026, 4, 1)),
        ("30 April 2026", date(2026, 4, 30)),
        ("April 30, 2026", date(2026, 4, 30)),
        ("by 9 August 2026 please", date(2026, 8, 9)),
        ("as soon as possible", None),
        ("14 days from receipt", None),
        ("before you travel", None),
        (None, None),
        ("2026-13-45", None),
    ],
)
def test_only_real_dates_are_parsed(text, expected) -> None:
    """Inventing a date for "as soon as possible" would put a fabricated deadline
    in front of the user."""
    assert OI.parse_when(text) == expected


def test_the_prefilter_is_deterministic_and_bounded() -> None:
    """Asking about every pair is quadratic; on real data it did not finish."""
    request = "proof of health insurance for the program period"
    candidates = [(i, f"document {i} about insurance coverage") for i in range(20)]
    first = OI.rank_candidates(request, candidates)
    second = OI.rank_candidates(request, candidates)
    assert first == second
    assert len(first) <= OI.MAX_CANDIDATES_PER_REQUEST


def test_a_candidate_sharing_no_words_is_never_asked_about() -> None:
    assert OI.rank_candidates("proof of insurance", [(1, "flight itinerary")]) == []


def test_a_satisfaction_verdict_is_cached(conn) -> None:
    calls = []

    def ask(_prompt, _model):
        calls.append(1)
        return '{"satisfied": true, "reason": "same thing"}'

    for _ in range(3):
        OI.satisfies(conn, "proof of insurance", "insurance.pdf", model="m", ask=ask)
    assert len(calls) == 1, "the model was asked the same pair twice"


# --- dismissal -------------------------------------------------------------


def _dismissable(conn, process):
    asked = _mail(conn, uid="ask", title="Please complete the portal declaration",
                  when="2026-04-01T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "the portal declaration", "due": None}])
    return asked


def test_a_dismissed_item_leaves_the_open_list(conn, process) -> None:
    _dismissable(conn, process)
    before = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": false}',
                        process_id=process.id)
    assert len(before.items) == 1

    OI.dismiss(conn, process.id, "the portal declaration", known_docs=before.documents_considered)
    after = OI.compute(conn, G.gather(conn, process), model="test-model",
                       today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": false}',
                       process_id=process.id)
    assert after.items == []
    assert len(after.settled_items) == 1
    assert after.settled_items[0].settled == "done"


def test_a_dismissal_is_evidence_not_a_delete(conn, process) -> None:
    """An absence would simply be re-derived as open on the next run."""
    _dismissable(conn, process)
    OI.dismiss(conn, process.id, "the portal declaration")
    report = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": false}',
                        process_id=process.id)
    settled = report.settled_items[0]
    assert settled.settled_at, "no date was recorded"
    assert settled.what == "the portal declaration"
    assert settled.doc_ids, "history was lost"


def test_settling_one_wording_settles_the_restatement(conn, process) -> None:
    """A checklist repeated in a reminder is one obligation."""
    for i, when in enumerate(("2026-04-01T09:00:00+00:00", "2026-04-08T09:00:00+00:00")):
        doc = _mail(conn, uid=f"ask{i}", title="Reminder", when=when, sender=THEM)
        _extract(conn, doc, direction="inbound",
                 requests_of_me=[{"what": "a copy of your passport", "due": None}])
    OI.dismiss(conn, process.id, "copy of the passport")   # different wording
    report = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": false}',
                        process_id=process.id)
    assert report.items == []


def test_new_documents_mark_a_dismissal_as_contradicted(conn, process) -> None:
    """The dismissal was a judgement about what was known then, so it is
    surfaced rather than silently kept or silently dropped."""
    _dismissable(conn, process)
    OI.dismiss(conn, process.id, "the portal declaration", known_docs=1)

    for i in range(3):
        extra = _mail(conn, uid=f"later{i}", title="More correspondence",
                      when="2026-04-20T09:00:00+00:00", sender=THEM)
        _extract(conn, extra, direction="inbound")

    report = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": false}',
                        process_id=process.id)
    assert report.settled_items[0].contradicted is True


def test_undismissing_returns_it_to_the_open_list(conn, process) -> None:
    _dismissable(conn, process)
    OI.dismiss(conn, process.id, "the portal declaration")
    assert OI.undismiss(conn, process.id, "the portal declaration") is True
    report = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": false}',
                        process_id=process.id)
    assert len(report.items) == 1


def test_a_passed_deadline_with_no_activity_asks_once(conn, process) -> None:
    """"Due 17 Aug, nothing since 2 Apr. Done, or still open?" — a question that
    can be answered, rather than a line repeated forever."""
    asked = _mail(conn, uid="due", title="Send it by 17 August",
                  when="2026-04-02T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "the signed agreement", "due": "2026-04-17"}])

    report = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": false}',
                        process_id=process.id)
    item = next(i for i in report.items if i.kind == OI.UNSATISFIED)
    assert item.needs_confirmation is True
    assert item.last_activity == "2026-04-02"


def test_a_deadline_that_the_process_moved_past_does_not_ask(conn, process) -> None:
    """Activity after the deadline answers the question by itself."""
    asked = _mail(conn, uid="due", title="Send it by 17 April",
                  when="2026-04-02T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "the signed agreement", "due": "2026-04-17"}])
    later = _mail(conn, uid="after", title="Thanks, received",
                  when="2026-04-25T09:00:00+00:00", sender=THEM)
    _extract(conn, later, direction="inbound")

    report = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": false}',
                        process_id=process.id)
    item = next(i for i in report.items if i.kind == OI.UNSATISFIED)
    assert item.needs_confirmation is False


def test_a_future_deadline_is_not_asked_about(conn, process) -> None:
    asked = _mail(conn, uid="future", title="Send it by June",
                  when="2026-04-02T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "the form", "due": "2026-06-30"}])
    report = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": false}',
                        process_id=process.id)
    assert all(not i.needs_confirmation for i in report.items)


# --- the cached-only mode, which is what makes an overview affordable ------


def test_cached_only_never_asks_the_model(conn, process) -> None:
    """The panel's resting state must not wait on an inference to draw itself."""
    asked = _mail(conn, uid="ask", title="Please send insurance",
                  when="2026-04-01T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "proof of insurance", "due": None}])
    replied = _mail(conn, uid="reply", title="Re: insurance",
                    when="2026-04-02T09:00:00+00:00", sender=OWN)
    _extract(conn, replied, direction="outbound",
             things_i_sent=[{"what": "proof of insurance certificate", "doc_ref": None}])

    def refuse(_prompt, _model):  # pragma: no cover — reaching this is the failure
        raise AssertionError("cached_only asked the model")

    report = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1), ask=refuse,
                        process_id=process.id, cached_only=True)
    assert report.estimated is True
    assert report.requests_satisfied == 0


def test_an_uncached_pair_counts_as_open_so_the_number_is_an_upper_bound(
    conn, process
) -> None:
    asked = _mail(conn, uid="ask", title="Please send insurance",
                  when="2026-04-01T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "proof of insurance", "due": None}])
    replied = _mail(conn, uid="reply", title="Re: insurance",
                    when="2026-04-02T09:00:00+00:00", sender=OWN)
    _extract(conn, replied, direction="outbound",
             things_i_sent=[{"what": "proof of insurance certificate", "doc_ref": None}])

    cached = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": true}',
                        process_id=process.id, cached_only=True)
    # Warm the judgement, then ask again with the model forbidden.
    OI.compute(conn, G.gather(conn, process), model="test-model",
               today=date(2026, 5, 1), ask=lambda _p, _m: '{"satisfied": true}',
               process_id=process.id)
    warm = OI.compute(conn, G.gather(conn, process), model="test-model",
                      today=date(2026, 5, 1),
                      ask=lambda _p, _m: (_ for _ in ()).throw(AssertionError("asked")),
                      process_id=process.id, cached_only=True)

    assert len(cached.items) >= len(warm.items)
    assert warm.estimated is False
    assert warm.requests_satisfied == 1


def test_nothing_to_judge_is_not_an_estimate(conn, process) -> None:
    """A report with no pairs to weigh knows its numbers exactly."""
    asked = _mail(conn, uid="ask", title="Please send insurance",
                  when="2026-04-01T09:00:00+00:00", sender=THEM)
    _extract(conn, asked, direction="inbound",
             requests_of_me=[{"what": "proof of insurance", "due": None}])
    report = OI.compute(conn, G.gather(conn, process), model="test-model",
                        today=date(2026, 5, 1),
                        ask=lambda _p, _m: (_ for _ in ()).throw(AssertionError("asked")),
                        process_id=process.id, cached_only=True)
    assert report.estimated is False
    assert [i.kind for i in report.items] == [OI.UNSATISFIED]
