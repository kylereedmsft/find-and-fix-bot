"""
Persistent resolution cache.

Keyed by (work scope, match.key) and written to
``%LOCALAPPDATA%/find-and-fix-bot/resolutions.json``. On startup the harness
hydrates each work unit's state from disk, so relaunching costs zero LLM calls
for matches whose surrounding code hasn't changed (the key embeds a content
hash of the focus span).

We do NOT persist the (re-derivable) `focus_code`; cached entries carry the
match metadata + the resolution (verdict, explanation, diff) only. We DO
persist the focus span's `content_hash` so a hydrated match reproduces the
exact key it was cached under (the key embeds that hash).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from .config import WorkConfig
from .models import AnalyzedMatch, Match, Resolution, Verdict


def _cache_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache")
    return base / "find-and-fix-bot" / "resolutions.json"


def _to_payload(a: AnalyzedMatch) -> dict:
    m, r = a.match, a.resolution
    return {
        "match": {
            "work": m.work, "path": m.path, "abs_path": m.abs_path,
            "line": m.line, "col": m.col, "matched_text": m.matched_text,
            "snippet": m.snippet, "focus_start": m.focus_start,
            "focus_end": m.focus_end, "refiner": m.refiner, "reason": m.reason,
            "occurrences": list(m.occurrences),
            "content_hash": m.content_hash,
        },
        "resolution": {
            "verdict": r.verdict.value, "explanation": r.explanation,
            "diff": r.diff, "confidence": r.confidence, "error": r.error,
            "file_sig": r.file_sig,
            "work_item_id": r.work_item_id, "work_item_url": r.work_item_url,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        },
    }


def _from_payload(d: dict, key: str | None = None) -> AnalyzedMatch:
    md = d["match"]
    # Prefer the explicitly persisted hash; fall back to the trailing hash of
    # the cache key for entries written before content_hash was persisted.
    stored = md.get("content_hash", "")
    if not stored and key and key.count(":") >= 2:
        stored = key.rsplit(":", 1)[-1]
    m = Match(
        work=md["work"], path=md["path"], abs_path=md["abs_path"],
        line=md["line"], col=md["col"], matched_text=md["matched_text"],
        snippet=md["snippet"], focus_start=md["focus_start"],
        focus_end=md["focus_end"], focus_code="", refiner=md["refiner"],
        reason=md.get("reason", ""),
        occurrences=tuple(md.get("occurrences", ())),
        stored_hash=stored,
    )
    rd = d["resolution"]
    r = Resolution(
        verdict=Verdict(rd["verdict"]), explanation=rd.get("explanation", ""),
        diff=rd.get("diff", ""), confidence=rd.get("confidence", ""),
        error=rd.get("error"), file_sig=rd.get("file_sig", ""),
        work_item_id=rd.get("work_item_id"), work_item_url=rd.get("work_item_url", ""),
        resolved_at=datetime.fromisoformat(rd["resolved_at"]) if rd.get("resolved_at") else None,
    )
    return AnalyzedMatch(match=m, resolution=r)


class ResolutionCache:
    """JSON-backed {match.key: AnalyzedMatch} store, scoped to one work unit."""

    def __init__(self, work: WorkConfig) -> None:
        self._path = _cache_path()
        self._scope = work.scope_key
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        self._data = doc.get(self._scope, {})

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            doc = {}
        doc[self._scope] = self._data
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self, key: str) -> AnalyzedMatch | None:
        raw = self._data.get(key)
        if raw is None:
            return None
        try:
            return _from_payload(raw, key)
        except Exception:  # noqa: BLE001 — corrupt entry
            return None

    def put(self, a: AnalyzedMatch) -> None:
        if a.verdict in (Verdict.PENDING, Verdict.ERROR):
            return  # don't persist transient states
        self._data[a.key] = _to_payload(a)
        self._save()

    def evict(self, key: str) -> None:
        """Drop one entry so the next investigation ignores the cache."""
        if self._data.pop(key, None) is not None:
            self._save()

    def clear(self) -> None:
        """Drop every entry for this work unit so the next scan re-investigates
        all matches from scratch (used by 'Re-eval tab')."""
        if self._data:
            self._data = {}
            self._save()

    def prune(self, live_keys: set[str]) -> None:
        before = len(self._data)
        self._data = {k: v for k, v in self._data.items() if k in live_keys}
        if len(self._data) != before:
            self._save()

    def hydrate(self) -> dict[str, AnalyzedMatch]:
        out: dict[str, AnalyzedMatch] = {}
        for k in self._data:
            a = self.get(k)
            if a is not None:
                out[k] = a
        return out
