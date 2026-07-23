"""Tests for the resolution cache and data models."""

from __future__ import annotations

from findfix.cache import ResolutionCache
from findfix.config import WorkConfig
from findfix.models import AnalyzedMatch, Match, Resolution, Verdict


def _match(key_suffix="v1") -> Match:
    return Match(
        work="t", path="a.py", abs_path="/tmp/a.py", line=3, col=1,
        matched_text="TODO", snippet="x", focus_start=1, focus_end=5,
        focus_code=f"code {key_suffix}", refiner="line-window",
    )


def _analyzed(verdict=Verdict.FIX, diff="--- a\n+++ b\n") -> AnalyzedMatch:
    return AnalyzedMatch(_match(), Resolution(verdict=verdict, explanation="e", diff=diff))


def test_cache_roundtrip():
    w = WorkConfig(label="t", regex="x")
    cache = ResolutionCache(w)
    a = _analyzed()
    cache.put(a)
    got = ResolutionCache(w).hydrate()  # fresh instance reads from disk
    assert a.key in got
    assert got[a.key].verdict == Verdict.FIX
    assert got[a.key].resolution.diff == a.resolution.diff


def test_cache_skips_transient_states():
    w = WorkConfig(label="t", regex="x")
    cache = ResolutionCache(w)
    cache.put(_analyzed(verdict=Verdict.PENDING))
    cache.put(_analyzed(verdict=Verdict.ERROR))
    assert ResolutionCache(w).hydrate() == {}


def test_cache_prune():
    w = WorkConfig(label="t", regex="x")
    cache = ResolutionCache(w)
    a = _analyzed()
    cache.put(a)
    cache.prune(live_keys=set())  # nothing live -> drop everything
    assert ResolutionCache(w).hydrate() == {}


def test_cache_scoped_by_work():
    a_work = WorkConfig(label="a", regex="x")
    b_work = WorkConfig(label="b", regex="y")
    ResolutionCache(a_work).put(_analyzed())
    # A different work scope shares the file but not the entries.
    assert ResolutionCache(b_work).hydrate() == {}


def test_verdict_ordering_fix_before_skip():
    keys = [Verdict.SKIP, Verdict.FIX, Verdict.APPLIED, Verdict.PENDING, Verdict.ERROR]
    ordered = sorted(keys, key=lambda v: v.sort_key)
    assert ordered[0] == Verdict.FIX
    assert ordered[-1] == Verdict.ERROR


def test_resolution_has_fix():
    assert Resolution(verdict=Verdict.FIX, diff="d").has_fix
    assert not Resolution(verdict=Verdict.FIX, diff="   ").has_fix  # blank diff
    assert not Resolution(verdict=Verdict.SKIP, diff="d").has_fix


def test_match_key_includes_content_hash():
    m1 = _match("v1")
    m2 = _match("v2")
    assert m1.key != m2.key
    assert m1.path in m1.key and str(m1.line) in m1.key
