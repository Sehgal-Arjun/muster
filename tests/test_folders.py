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
