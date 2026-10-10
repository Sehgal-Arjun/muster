"""Directories offered beside a search, never inside it.

A folder has no content, so ranking it against documents would mean inventing a
score. These tests are mostly about it staying out of the way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from muster.folders import replace_all, scan, suggest


@pytest.fixture
def mapped(conn, tmp_path):
    (tmp_path / "GitHub" / "muster").mkdir(parents=True)
    (tmp_path / "GitHub" / "muster" / "muster").mkdir()
    (tmp_path / "Documents" / "Taxes").mkdir(parents=True)
    (tmp_path / "node_modules" / "junk").mkdir(parents=True)
    (tmp_path / ".hidden" / "secret").mkdir(parents=True)
    replace_all(conn, scan((str(tmp_path),), {"node_modules"}))
    return conn


def test_a_name_finds_its_folder(mapped) -> None:
    hits = suggest(mapped, "muster")
    assert hits, "no suggestion for an exact folder name"
    assert hits[0].name == "muster"
    assert hits[0].path.endswith("GitHub/muster"), "the shallower one should win"


def test_the_shallower_folder_ranks_first(mapped) -> None:
    """Of two folders with the same name, the outer one is nearly always meant."""
    exact = [h for h in suggest(mapped, "muster") if h.name == "muster"]
    assert len(exact) == 2
    assert [h.depth for h in exact] == sorted(h.depth for h in exact)


def test_an_exact_name_outranks_a_shallower_near_miss(mapped, tmp_path) -> None:
    """"muster" should not be beaten by "muster-home" for being one level up."""
    (tmp_path / "muster-adjacent").mkdir()
    replace_all(mapped, scan((str(tmp_path),), {"node_modules"}))

    hits = suggest(mapped, "muster")
    assert hits[0].name == "muster", [h.name for h in hits]


def test_a_sentence_gets_no_suggestion(mapped) -> None:
    """A question about content answered with a folder is noise."""
    assert suggest(mapped, "warehouse lease renewal agreement signed") == []
    assert suggest(mapped, "") == []


def test_noise_directories_are_never_offered(mapped) -> None:
    assert suggest(mapped, "junk") == []
    assert suggest(mapped, "secret") == [], "hidden directories are not jump targets"


def test_partial_names_match(mapped) -> None:
    assert any(h.name == "Taxes" for h in suggest(mapped, "tax"))


def test_the_display_path_is_written_the_way_people_say_it(conn, tmp_path) -> None:
    from muster.folders import Folder

    home = str(Path.home())
    assert Folder(path=f"{home}/GitHub/muster", name="muster", depth=2, mtime=None).display == (
        "~/GitHub/muster"
    )


def test_depth_is_bounded(conn, tmp_path) -> None:
    deep = tmp_path
    for level in range(8):
        deep = deep / f"level{level}"
    deep.mkdir(parents=True)
    found = scan((str(tmp_path),), set(), max_depth=3)
    assert found, "nothing scanned at all"
    assert max(s.folder.depth for s in found) <= 3


def test_a_rescan_forgets_folders_that_are_gone(conn, tmp_path) -> None:
    (tmp_path / "temporary").mkdir()
    replace_all(conn, scan((str(tmp_path),), set()))
    assert suggest(conn, "temporary")

    (tmp_path / "temporary").rmdir()
    replace_all(conn, scan((str(tmp_path),), set()))
    assert suggest(conn, "temporary") == []


# --- matching the way people actually type --------------------------------


@pytest.fixture
def course_tree(conn, tmp_path):
    """The shape that broke every assumption the first version made."""
    (tmp_path / "Waterloo" / "3A" / "MATH213" / "Sample Exams").mkdir(parents=True)
    (tmp_path / "Waterloo" / "3A" / "SE350" / "past exams").mkdir(parents=True)
    replace_all(conn, scan((str(tmp_path),), set()))
    return conn


def test_a_run_together_name_is_found_as_separate_words(course_tree) -> None:
    """MATH213 is one word on disk and two as typed."""
    assert any(h.name == "MATH213" for h in suggest(course_tree, "math 213"))
    assert any(h.name == "MATH213" for h in suggest(course_tree, "math213"))


def test_a_folder_is_reachable_by_naming_what_it_sits_under(course_tree) -> None:
    """"Sample Exams" is remembered as the course far more often than as itself."""
    hits = suggest(course_tree, "math 213 sample exam")
    assert hits and hits[0].name == "Sample Exams", [h.display for h in hits]


def test_word_order_does_not_matter(course_tree) -> None:
    """The words are the memory; the order is not."""
    assert any(h.name == "Sample Exams" for h in suggest(course_tree, "exams math"))
    assert any(h.name == "past exams" for h in suggest(course_tree, "se350 exams past"))


def test_half_typed_words_still_match(course_tree) -> None:
    assert any(h.name == "Sample Exams" for h in suggest(course_tree, "sampl exam"))


def test_every_word_must_appear(course_tree) -> None:
    """AND, not OR: matching any one word would offer whatever shares a term."""
    assert suggest(course_tree, "math 213 chemistry") == []


def test_a_sentence_is_still_a_question_about_content(course_tree) -> None:
    assert suggest(course_tree, "what did the exam say about linear algebra proofs") == []


def test_the_folders_own_name_outranks_its_ancestors(course_tree) -> None:
    hits = suggest(course_tree, "past exams")
    assert hits[0].name == "past exams"


def test_word_splitting_covers_the_forms_people_type() -> None:
    from muster.folders import words_of

    assert words_of("MATH213") == ["MATH213", "MATH", "213"]
    assert words_of("past-exam-collection") == ["past", "exam", "collection"]
    assert [w.lower() for w in words_of("SampleExams")] == ["sampleexams", "sample", "exams"]


# --- meaning ---------------------------------------------------------------


def test_the_meaning_arm_is_optional(course_tree) -> None:
    """No model, no vectors, no problem: the word arm stands alone."""
    hits = suggest(course_tree, "math 213 sample exam", model_id="no-such-model")
    assert hits and hits[0].name == "Sample Exams"


def test_folder_texts_are_only_embedded_once(conn, tmp_path) -> None:
    """Rebuilt maps must not re-embed a thousand unchanged names every walk."""
    from muster.folders import pending_vectors, store_vectors

    (tmp_path / "Waterloo" / "MATH213" / "Sample Exams").mkdir(parents=True)
    scanned = scan((str(tmp_path),), set())
    replace_all(conn, scanned)

    first = pending_vectors(conn, "test-model")
    assert first, "nothing was pending on a fresh map"
    store_vectors(conn, "test-model", [(h, b"\x00" * 8) for h, _t in first])
    assert pending_vectors(conn, "test-model") == []

    # A rebuild of the same tree changes no text, so nothing needs embedding.
    replace_all(conn, scan((str(tmp_path),), set()))
    assert pending_vectors(conn, "test-model") == []


def test_a_changed_folder_name_needs_a_new_vector(conn, tmp_path) -> None:
    from muster.folders import pending_vectors, store_vectors

    (tmp_path / "Exams").mkdir()
    replace_all(conn, scan((str(tmp_path),), set()))
    store_vectors(
        conn, "test-model",
        [(h, b"\x00" * 8) for h, _t in pending_vectors(conn, "test-model")],
    )

    (tmp_path / "Exams").rename(tmp_path / "Tests")
    replace_all(conn, scan((str(tmp_path),), set()))
    assert pending_vectors(conn, "test-model"), "a renamed folder kept its old vector"


def test_the_meaning_text_reads_as_a_path(conn, tmp_path) -> None:
    (tmp_path / "Waterloo" / "MATH213" / "Sample Exams").mkdir(parents=True)
    scanned = scan((str(tmp_path),), set())
    deepest = max(scanned, key=lambda s: s.folder.depth)
    assert deepest.meaning_text.endswith("Sample Exams")
    assert " / " in deepest.meaning_text


# --- what a file says about where it belongs -------------------------------


def test_a_course_code_written_as_two_words_becomes_the_one_on_disk() -> None:
    """The document says "ENVS 205"; the folder is called ENVS205. Joining the
    pair is the difference between reading a course code and reading two tokens."""
    from muster.placing import content_terms

    terms = content_terms("ENVS 205 - Assignment 7. Socio-ecological impact of ENVS 205.")
    assert "ENVS205" in terms


def test_a_year_with_something_stuck_to_it_is_not_an_identifier() -> None:
    """"about 166" and "by 2000" became identifiers and matched folders with total
    confidence."""
    from muster.placing import content_terms

    terms = content_terms("There were about 166 responses by 2000 in 2026a.")
    assert not any(t.lower() in {"about166", "by2000", "2026a"} for t in terms)


def test_frequency_is_counted_rather_than_deduplicated() -> None:
    """The first version used folders.words_of, which dedupes — so every count was
    one and no term ever qualified."""
    from muster.placing import content_terms

    terms = content_terms("lease lease lease deposit deposit landlord")
    assert terms[:2] == ["lease", "deposit"]


def test_a_passing_mention_is_not_what_a_document_is_about() -> None:
    from muster.placing import content_terms

    assert "tangent" not in content_terms("A lease about a lease. Also a tangent.")
