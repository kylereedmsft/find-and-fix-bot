"""Tests for the deterministic scanner + refiners (`findfix.scanner`)."""

from __future__ import annotations

import pytest

from findfix.config import WorkConfig
from findfix.scanner import ScanError, Scanner, group_by_file


def _work(tmp_path, **kw) -> WorkConfig:
    kw.setdefault("include", ("**/*.py",))
    return WorkConfig(label="t", root=str(tmp_path), **kw)


def test_regex_scan_finds_line_and_col(tmp_path):
    (tmp_path / "a.py").write_text("ok = 1\nbad = TODO_HERE\n", encoding="utf-8")
    matches = Scanner(_work(tmp_path, regex=r"TODO_HERE")).scan()
    assert len(matches) == 1
    m = matches[0]
    assert m.path == "a.py"
    assert m.line == 2
    assert m.col == 7  # 1-based column of the match start
    assert m.matched_text == "TODO_HERE"


def test_excludes_common_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "good.py").write_text("PATTERN\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "bad.py").write_text("PATTERN\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "bad.py").write_text("PATTERN\n", encoding="utf-8")
    matches = Scanner(_work(tmp_path, regex="PATTERN")).scan()
    assert [m.path for m in matches] == ["src/good.py"]


def test_custom_exclude_glob(tmp_path):
    (tmp_path / "keep.py").write_text("HIT\n", encoding="utf-8")
    (tmp_path / "gen.py").write_text("HIT\n", encoding="utf-8")
    w = _work(tmp_path, regex="HIT", exclude=("gen.py",))
    assert [m.path for m in Scanner(w).scan()] == ["keep.py"]


def test_max_matches_cap(tmp_path):
    (tmp_path / "a.py").write_text("X\n" * 50, encoding="utf-8")
    w = _work(tmp_path, regex="X", max_matches=5)
    assert len(Scanner(w).scan()) == 5


def test_include_filter(tmp_path):
    (tmp_path / "a.py").write_text("HIT\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("HIT\n", encoding="utf-8")
    w = WorkConfig(label="t", root=str(tmp_path), include=("**/*.py",), regex="HIT")
    assert [m.path for m in Scanner(w).scan()] == ["a.py"]


def test_line_window_refiner_span(tmp_path):
    body = "\n".join(f"line{i}" for i in range(1, 21)) + "\nTARGET\n"
    (tmp_path / "a.py").write_text(body, encoding="utf-8")
    w = _work(tmp_path, regex="TARGET", refiner="line-window", context_lines=3)
    m = Scanner(w).scan()[0]
    assert m.refiner == "line-window"
    assert m.focus_start == m.line - 3
    assert m.focus_end == m.line  # TARGET is the last line
    assert "TARGET" in m.focus_code


def test_treesitter_refiner_widens_to_block(tmp_path):
    src = (
        "import os\n\n"
        "def handler():\n"
        "    try:\n"
        "        do_it()\n"
        "    except:\n"
        "        pass\n"
    )
    (tmp_path / "a.py").write_text(src, encoding="utf-8")
    w = _work(tmp_path, regex=r"except\s*:", refiner="treesitter", language="python")
    m = Scanner(w).scan()[0]
    # Whether or not tree-sitter is installed the scan must succeed; when it is,
    # the focus widens to the enclosing block rather than a symmetric window.
    assert m.refiner in ("treesitter", "line-window")
    if m.refiner == "treesitter":
        assert m.focus_start <= 4  # includes the `try:`
        assert "except:" in m.focus_code


def test_description_only_scan_returns_empty(tmp_path):
    (tmp_path / "a.py").write_text("whatever\n", encoding="utf-8")
    w = WorkConfig(label="t", root=str(tmp_path), description="find conceptual stuff")
    assert Scanner(w).scan() == []


def test_bad_regex_raises_scan_error(tmp_path):
    with pytest.raises(ScanError):
        Scanner(_work(tmp_path, regex="(unclosed"))


def test_missing_root_raises(tmp_path):
    w = WorkConfig(label="t", root=str(tmp_path / "does-not-exist"), regex="x")
    with pytest.raises(ScanError):
        Scanner(w).scan()


def test_content_hash_changes_key(tmp_path):
    (tmp_path / "a.py").write_text("HIT\ncontext_v1\n", encoding="utf-8")
    k1 = Scanner(_work(tmp_path, regex="HIT", refiner="line-window", context_lines=2)).scan()[0].key
    (tmp_path / "a.py").write_text("HIT\ncontext_v2_changed\n", encoding="utf-8")
    k2 = Scanner(_work(tmp_path, regex="HIT", refiner="line-window", context_lines=2)).scan()[0].key
    assert k1 != k2  # surrounding code changed -> re-investigate


def test_group_by_file_collapses_multi_hits(tmp_path):
    (tmp_path / "a.py").write_text("X = 1\nX = 2\nX = 3\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("X = 9\n", encoding="utf-8")
    raw = Scanner(_work(tmp_path, regex=r"X = \d")).scan()
    assert len(raw) == 4  # scanner still emits one match per hit
    groups = group_by_file(raw)
    assert len(groups) == 2  # one investigation per file
    by_path = {g.path: g for g in groups}
    assert by_path["a.py"].occurrences == (1, 2, 3)
    assert by_path["a.py"].occurrence_count == 3
    assert by_path["b.py"].occurrence_count == 1


def test_group_by_file_dedups_same_line_hits(tmp_path):
    (tmp_path / "a.py").write_text("X and X and X\n", encoding="utf-8")
    groups = group_by_file(Scanner(_work(tmp_path, regex="X")).scan())
    assert len(groups) == 1
    assert groups[0].occurrences == (1,)  # three hits, same line -> one occurrence


def test_group_by_file_merges_focus_spans(tmp_path):
    body = "HIT\n" + "\n".join(f"L{i}" for i in range(1, 30)) + "\nHIT\n"
    (tmp_path / "a.py").write_text(body, encoding="utf-8")
    groups = group_by_file(
        Scanner(_work(tmp_path, regex="HIT", refiner="line-window", context_lines=1)).scan()
    )
    assert len(groups) == 1
    g = groups[0]
    assert g.occurrence_count == 2
    assert g.focus_code.count("HIT") == 2  # merged focus spans both hits
    assert "occurrence at line" in g.focus_code
