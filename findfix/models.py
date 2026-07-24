"""Typed data flowing through the harness.

The boundary between "facts we scanned" (`Match`) and "judgment the LLM
added" (`Resolution.verdict` / `.explanation` / `.diff`) is deliberate and
visible in the types — exactly as pr_sentry separates `PullRequest` from
the reviewer's `Verdict`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Verdict(str, Enum):
    FIX = "fix"          # a real issue, a fix is proposed
    APPLIED = "applied"  # the proposed fix has been written to the working tree
    SKIP = "skip"        # investigated, no action needed (false positive / already fine)
    PENDING = "pending"  # queued / under investigation
    ERROR = "error"

    @property
    def glyph(self) -> str:
        return {"fix": "F", "applied": "A", "skip": "-", "pending": ".", "error": "!"}[self.value]

    @property
    def style(self) -> str:
        return {
            "fix": "bold white on red",
            "applied": "bold white on dark_green",
            "skip": "dim",
            "pending": "dim",
            "error": "bold white on magenta",
        }[self.value]

    @property
    def sort_key(self) -> int:
        # Actionable fixes first, then applied, then pending, skips, errors last.
        return {"fix": 0, "applied": 1, "pending": 2, "skip": 3, "error": 4}[self.value]


@dataclass(slots=True)
class Match:
    """A candidate pattern occurrence found by the deterministic scan.

    `focus_start`/`focus_end` bound the slice of the file we hand to the
    LLM. By default that's a line window around the hit; a structural
    refiner (TreeSitter/Roslyn) can widen it to the enclosing function/node
    so the model sees a self-contained unit.
    """

    work: str          # WorkConfig.label this match belongs to
    path: str          # path relative to the work root
    abs_path: str      # absolute path on disk
    line: int          # 1-based line of the hit
    col: int           # 1-based column of the hit
    matched_text: str  # the exact substring the regex matched
    snippet: str       # a few lines of context around the hit (for the table/detail)
    focus_start: int   # 1-based first line of the slice handed to the LLM
    focus_end: int     # 1-based last line (inclusive) of that slice
    focus_code: str    # the source text of [focus_start, focus_end]
    refiner: str = "line-window"  # which refiner produced the focus span
    reason: str = ""   # for NL/discovery matches: why the model flagged it
    occurrences: tuple[int, ...] = ()  # all hit lines in this file when grouped; empty => single hit at `line`
    stored_hash: str = ""  # persisted content hash for cache-hydrated matches (focus_code isn't persisted)

    @property
    def occurrence_count(self) -> int:
        return len(self.occurrences) or 1

    @property
    def content_hash(self) -> str:
        # Live matches hash their focus span; cache-hydrated matches (which
        # intentionally don't persist focus_code) fall back to the stored hash
        # so `key` stays identical to the key they were cached under.
        if self.focus_code:
            return hashlib.sha1(self.focus_code.encode("utf-8", "replace")).hexdigest()[:12]
        if self.stored_hash:
            return self.stored_hash
        return hashlib.sha1(b"").hexdigest()[:12]

    @property
    def key(self) -> str:
        """Stable identity for change detection + caching.

        Includes a content hash so that if the surrounding code changes the
        match is re-investigated rather than served from cache.
        """
        return f"{self.path}:{self.line}:{self.content_hash}"


@dataclass(slots=True)
class Resolution:
    """The LLM's judgment about a `Match`."""

    verdict: Verdict
    explanation: str = ""
    diff: str = ""              # git-format unified diff, empty when SKIP/ERROR
    confidence: str = ""        # high | medium | low
    error: str | None = None
    resolved_at: datetime | None = None
    file_sig: str = ""          # whole-file hash at resolution time; drives stale detection
    work_item_id: int | None = None  # ADO work item filed for this match (per-file); None => untracked
    work_item_url: str = ""     # browser URL of the filed work item, when known

    @property
    def has_fix(self) -> bool:
        return bool(self.diff.strip()) and self.verdict in (Verdict.FIX, Verdict.APPLIED)

    @property
    def is_tracked(self) -> bool:
        return self.work_item_id is not None


@dataclass(slots=True)
class AnalyzedMatch:
    """A `Match` plus the LLM's `Resolution` of it."""

    match: Match
    resolution: Resolution
    stale: bool = False   # file changed on disk since this was resolved (recomputed each scan; not persisted)

    @property
    def key(self) -> str:
        return self.match.key

    @property
    def verdict(self) -> Verdict:
        return self.resolution.verdict


@dataclass(slots=True)
class AppState:
    """Everything the TUI needs to render one work unit's tab."""

    items: dict[str, AnalyzedMatch] = field(default_factory=dict)  # match.key -> analyzed
    analysing_key: str | None = None       # match currently in front of Copilot
    analysis_enabled: bool = True          # SPACE toggles; always starts on
    last_refresh: datetime | None = None
    status: str = "starting"
    scanning: bool = False
    error: str | None = None

    @property
    def pending_count(self) -> int:
        return sum(1 for a in self.items.values() if a.verdict == Verdict.PENDING)

    @property
    def fix_count(self) -> int:
        return sum(1 for a in self.items.values() if a.verdict == Verdict.FIX)

    @property
    def applied_count(self) -> int:
        return sum(1 for a in self.items.values() if a.verdict == Verdict.APPLIED)

    def ordered(self) -> list[AnalyzedMatch]:
        return sorted(
            self.items.values(),
            key=lambda a: (a.verdict.sort_key, a.match.path, a.match.line),
        )
