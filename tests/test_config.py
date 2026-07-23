"""Tests for the JSON work-config loader (`findfix.config`)."""

from __future__ import annotations

import json

import pytest

from findfix.config import DEFAULT_WORK, WorkConfig, load_work_configs


def _write(tmp_path, doc) -> str:
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def test_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no findfix.config.json here
    works = load_work_configs()
    assert [w.label for w in works] == [w.label for w in DEFAULT_WORK]


def test_missing_explicit_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_work_configs(tmp_path / "nope.json")


def test_accepts_top_level_list(tmp_path):
    path = _write(tmp_path, [{"label": "a", "regex": "x"}])
    works = load_work_configs(path)
    assert len(works) == 1 and works[0].label == "a"


def test_accepts_work_key_object(tmp_path):
    path = _write(tmp_path, {"work": [{"label": "a", "regex": "x"}]})
    assert load_work_configs(path)[0].label == "a"


def test_empty_list_raises(tmp_path):
    with pytest.raises(ValueError):
        load_work_configs(_write(tmp_path, {"work": []}))


def test_missing_label_raises(tmp_path):
    with pytest.raises(ValueError):
        load_work_configs(_write(tmp_path, [{"regex": "x"}]))


def test_include_string_coerced_to_tuple(tmp_path):
    w = load_work_configs(_write(tmp_path, [{"label": "a", "regex": "x", "include": "**/*.py"}]))[0]
    assert w.include == ("**/*.py",)


def test_description_only_detection():
    assert WorkConfig(label="a", description="find stuff").is_description_only
    assert not WorkConfig(label="a", regex="x", description="d").is_description_only


def test_scope_key_stable_and_pattern_sensitive():
    a = WorkConfig(label="a", regex="x")
    b = WorkConfig(label="a", regex="x")
    c = WorkConfig(label="a", regex="y")
    assert a.scope_key == b.scope_key
    assert a.scope_key != c.scope_key


def test_always_excludes_present():
    w = WorkConfig(label="a", regex="x", exclude=("custom/**",))
    assert "custom/**" in w.all_excludes
    assert any(".git" in e for e in w.all_excludes)


def test_example_config_loads():
    works = load_work_configs("findfix.config.example.json")
    assert {w.label for w in works} == {"TODO/FIXME", "bare-except", "sync-file-io"}
    # the sync-file-io unit is description-only and grants an ADO MCP server
    sfi = next(w for w in works if w.label == "sync-file-io")
    assert sfi.is_description_only
    assert "ado" in sfi.mcp
