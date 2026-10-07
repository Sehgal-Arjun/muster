

def test_html_is_read_as_text_not_as_source() -> None:
    """An exported notebook is otherwise indexed as its own doctype and scripts.

    One such file produced 1,375 chunks of markup; 540 of them were two thirds
    of everything waiting to be embedded, none of it text anybody would search.
    """
    from muster.extract import extract_text

    page = b"""<!DOCTYPE html>
    <html><head><title>Fuzzing</title>
      <style>.reveal { color: red; }</style>
      <script>var x = 1; function noise(){ return "not text"; }</script>
    </head>
    <body><h1>Fuzzing In The Large</h1>
      <p>Grammar-based fuzzing finds bugs faster.</p>
    </body></html>"""

    out = extract_text(page, "text/html", "slides.html")

    assert "Fuzzing In The Large" in out
    assert "Grammar-based fuzzing finds bugs faster." in out
    assert "doctype" not in out.lower()
    assert "function noise" not in out, "script contents reached the index"
    assert "color: red" not in out, "stylesheet reached the index"


def test_html_extraction_falls_back_rather_than_indexing_nothing(monkeypatch) -> None:
    from muster import extract as extract_module

    def boom(_source):
        raise ValueError("malformed")

    monkeypatch.setattr("muster.htmltext.html_to_text", boom)
    out = extract_module.extract_text(b"<p>hello there</p>", "text/html", "x.html")
    assert "hello there" in out


def test_extract_all_visits_every_process(conn, cfg, monkeypatch) -> None:
    """Bumping the extractor invalidates every reading at once; re-running it one
    process at a time is a step nobody should have to remember."""
    from muster import cli

    visited: list[str] = []
    real = cli.process_extract

    def spy(slug, model=None, limit=0):
        if slug == "all":
            return real(slug, model=model, limit=limit)
        visited.append(slug)

    monkeypatch.setattr(cli, "process_extract", spy)
    # The recursion goes through the module attribute, so the spy sees each call.
    monkeypatch.setattr(
        "muster.processes.all_processes",
        lambda _conn: [
            type("P", (), {"slug": "one", "name": "One"})(),
            type("P", (), {"slug": "two", "name": "Two"})(),
        ],
    )
    real("all")
    assert visited == ["one", "two"]
