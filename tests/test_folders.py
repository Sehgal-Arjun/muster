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
    assert max(f.depth for f in found) <= 3


def test_a_rescan_forgets_folders_that_are_gone(conn, tmp_path) -> None:
    (tmp_path / "temporary").mkdir()
    replace_all(conn, scan((str(tmp_path),), set()))
    assert suggest(conn, "temporary")

    (tmp_path / "temporary").rmdir()
    replace_all(conn, scan((str(tmp_path),), set()))
    assert suggest(conn, "temporary") == []
