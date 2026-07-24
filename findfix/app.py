"""
ORCHESTRATION — the harness, one instance per unit of work.

Owns the scan loop, change detection, the resolution cache, and fix
application. Each tick:

  1. Deterministic scan (regex units) or cached discovery (description-only
     units) -> the current set of candidate matches.
  2. Drop resolutions for matches that vanished; prune the cache likewise.
  3. For any *new* match, check the disk cache (keyed by a content hash of
     the focus span); on a miss, hand it to Copilot. One match at a time.

With N work units configured that's up to N Copilot sessions in flight
concurrently, each an independent worker. SPACE pauses/resumes a unit's
queue. `apply_fix` writes a proposed diff to the working tree via `git apply`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json as _json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .cache import ResolutionCache
from . import ado
from .chat import ChatSession, history_path
from .config import WorkConfig
from .investigator import Investigator
from .models import AnalyzedMatch, AppState, Match, Resolution, Verdict
from .scanner import ScanError, Scanner, group_by_file, _matches_any
from .investigator import _resolution_from_item

StateListener = Callable[[], None]


class FindFixApp:
    def __init__(self, work: WorkConfig, interval: int = 60, model: str | None = None) -> None:
        self.work = work
        self.interval = interval
        self.state = AppState()
        self._scanner = Scanner(work)
        self._investigator = Investigator(work, model=model)
        self._cache = ResolutionCache(work)
        self._listeners: list[StateListener] = []
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._force_discovery = False
        self._chats: dict[str, ChatSession] = {}
        # Warm-start from disk so relaunching shows prior resolutions instantly.
        self.state.items = self._cache.hydrate()
        self._drop_excluded()  # honor current excludes even for cached/applied items
        if self.state.items:
            self.state.status = f"loaded {len(self.state.items)} cached result(s)"

    # ---- observers -------------------------------------------------------

    def on_change(self, fn: StateListener) -> None:
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in self._listeners:
            fn()

    def _status(self, msg: str) -> None:
        self.state.status = msg
        self._notify()

    # ---- controls --------------------------------------------------------

    def toggle_analysis(self) -> None:
        self.state.analysis_enabled = not self.state.analysis_enabled
        if self.state.analysis_enabled:
            self._status("analysis enabled")
            self._wake.set()
        else:
            self._status("analysis paused")
        self._notify()

    async def refresh_now(self) -> None:
        if not self.state.scanning:
            self._force_discovery = True  # let a manual refresh re-run NL discovery
            self._wake.set()

    def stop(self) -> None:
        self._stop.set()

    # ---- fix application -------------------------------------------------

    def apply_fix(self, key: str) -> tuple[bool, str]:
        """Apply a proposed diff to the working tree via `git apply`.

        Returns (ok, message). On success the item flips to APPLIED and the
        cache is updated.
        """
        a = self.state.items.get(key)
        if a is None or not a.resolution.has_fix:
            return False, "no applicable fix"
        ok, msg = _git_apply(self.work.root_path, a.resolution.diff)
        if ok:
            a.resolution.verdict = Verdict.APPLIED
            self._cache.put(a)
            self._status(f"applied fix to {a.match.path}")
            self._notify()
        else:
            self._status(f"apply failed: {msg}")
            self._notify()
        return ok, msg

    # ---- ADO work-item tracking -----------------------------------------

    def create_work_item(self, key: str) -> tuple[bool, str]:
        """File one ADO work item for the selected match's file.

        Idempotent: if the item already carries a `work_item_id` this is a
        no-op that just reports the existing id. Requires `ado_tracking` on the
        work unit. Returns (ok, message). On success the id/url are persisted.
        """
        a = self.state.items.get(key)
        if a is None:
            return False, "no such item"
        if a.resolution.is_tracked:
            wid = a.resolution.work_item_id
            self._status(f"already tracked as #{wid}")
            self._notify()
            return True, f"already tracked as #{wid}"
        tracking = self.work.ado_tracking
        if tracking is None:
            return False, "ADO tracking not configured for this work unit"

        title = ado.build_title(tracking, self.work.label, a)
        description = ado.build_description(self.work.label, a)
        self._status(f"filing work item for {a.match.path}…")
        self._notify()
        res = ado.create_work_item(tracking, title, description)
        if res.ok and res.work_item_id is not None:
            a.resolution.work_item_id = res.work_item_id
            a.resolution.work_item_url = res.url
            self._cache.put(a)
            self._status(f"filed work item #{res.work_item_id} for {a.match.path}")
            self._notify()
            return True, res.message
        self._status(f"work item failed: {res.message}")
        self._notify()
        return False, res.message

    # ---- interactive discussion -----------------------------------------

    async def chat_session(self, key: str) -> ChatSession | None:
        """Return the live discussion for one match, starting it lazily.

        One persistent Copilot session per match: created on first 'd', kept
        alive for the process, its transcript persisted to disk so discussion
        work survives restarts. When the model proposes a revised fix, the
        harness updates the resolution and cache in place.
        """
        a = self.state.items.get(key)
        if a is None:
            return None
        existing = self._chats.get(key)
        if existing is not None and existing.started:
            return existing

        session = existing or ChatSession(
            title=f"{self.work.label}: {a.match.path}",
            fix_handler=lambda payload, k=key: self._apply_chat_fix(k, payload),
            history=history_path(self.work.scope_key, key),
        )
        self._chats[key] = session
        preload = self._chat_context(a)
        await session.start(
            self._investigator.client,
            preload,
            **self._investigator.chat_kwargs(),
        )
        return session

    def _chat_context(self, a: AnalyzedMatch) -> str:
        m, r = a.match, a.resolution
        occ = ", ".join(str(x) for x in (m.occurrences or (m.line,)))
        parts = [
            f"PATTERN (label): {self.work.label}",
            f"regex: {self.work.regex or '(none)'}",
            f"description: {self.work.description or '(none)'}",
            "",
            f"FILE: {m.path}",
            f"occurrence line(s): {occ}",
            f"focus span: lines {m.focus_start}-{m.focus_end} (refiner: {m.refiner})",
            "",
            "FOCUSED CODE:",
            m.focus_code or "(unavailable)",
            "",
            "CURRENTLY PROPOSED FIX:",
            f"verdict: {r.verdict.name}",
            f"confidence: {r.confidence or '(n/a)'}",
            f"explanation: {r.explanation or '(none)'}",
            "diff:",
            r.diff or "(none)",
        ]
        if self.work.context:
            parts += ["", "UNIT GUIDANCE:", self.work.context.strip()]
        return "\n".join(parts)

    async def _apply_chat_fix(self, key: str, payload: str) -> str:
        """A discussion produced a revised fix — replace the proposal.

        Persisted to the cache so a re-scan restores the refined resolution
        instead of the model's original.
        """
        a = self.state.items.get(key)
        if a is None:
            return "no such match to update"
        try:
            data = _json.loads(payload)
        except Exception as e:  # noqa: BLE001
            return f"could not parse revised fix: {type(e).__name__}: {e}"
        new_res = _resolution_from_item(data)
        new_res.file_sig = _file_sig(a.match.abs_path)
        a.resolution = new_res
        a.stale = False
        self._cache.put(a)
        self._notify()
        v = new_res.verdict.name
        return f"Proposed fix updated (verdict={v}). Press 'a' in the table to apply it."

    # ---- core loop -------------------------------------------------------

    async def _scan_once(self) -> None:
        self.state.scanning = True
        self.state.error = None
        self._notify()
        try:
            if self.work.is_description_only:
                await self._discovery_cycle()
            else:
                await self._regex_cycle()
            self.state.last_refresh = datetime.now(timezone.utc)
            if self.state.analysis_enabled:
                self._status("idle")
        except ScanError as e:
            self.state.error = str(e)
        except Exception as e:  # noqa: BLE001
            self.state.error = f"{type(e).__name__}: {e}"
        finally:
            self.state.scanning = False
            self._notify()

    async def _regex_cycle(self) -> None:
        self._status(f"scanning {self.work.label}…")
        self._drop_excluded()  # a config edit may have added excludes since last cycle
        matches = await asyncio.to_thread(self._scanner.scan)
        # Investigate each file once: collapse all hits in a file into a single
        # grouped match so we don't redundantly re-investigate (and re-fix) the
        # same file for every occurrence.
        matches = group_by_file(matches)
        live = {m.key: m for m in matches}

        # Drop and prune vanished matches (unless applied — keep those visible).
        for gone in set(self.state.items) - set(live):
            if self.state.items[gone].verdict != Verdict.APPLIED:
                del self.state.items[gone]
        self._cache.prune(set(live))

        needs: list[Match] = []
        for key, m in live.items():
            if key in self.state.items and self.state.items[key].verdict != Verdict.PENDING:
                continue  # already resolved this exact content
            cached = self._cache.get(key)
            if cached is not None:
                self.state.items[key] = cached
                continue
            self.state.items[key] = AnalyzedMatch(m, Resolution(verdict=Verdict.PENDING))
            needs.append(m)
        self._mark_stale()
        self._notify()

        if not self.state.analysis_enabled:
            return
        await self._investigate_all(needs)

    def _drop_excluded(self) -> None:
        """Remove items whose path matches the CURRENT excludes (or no longer
        matches includes), evicting them from the cache too.

        Needed because `exclude`/`include` are not part of `scope_key`, so a
        cache hydrated at launch — or an `APPLIED` item we deliberately keep
        visible after it vanishes from a scan — can still carry entries from
        folders the config now excludes. This makes the work list honor the
        current filters immediately, regardless of verdict.
        """
        ex = self.work.all_excludes
        inc = self.work.include
        for key in list(self.state.items):
            path = self.state.items[key].match.path
            if _matches_any(path, ex) or (inc and not _matches_any(path, inc)):
                del self.state.items[key]
                self._cache.evict(key)

    def _mark_stale(self) -> None:
        """Flag resolved items whose file changed on disk since resolution.

        The match key already re-investigates when the *focus span* changes;
        this catches changes elsewhere in the file (e.g. after a repo sync) that
        the key wouldn't notice. Recomputed every scan, never persisted.
        """
        for a in self.state.items.values():
            r = a.resolution
            if r.verdict in (Verdict.PENDING, Verdict.ERROR) or not r.file_sig:
                a.stale = False
                continue
            cur = _file_sig(a.match.abs_path)
            a.stale = bool(cur and cur != r.file_sig)

    async def reevaluate(self, key: str) -> tuple[bool, str]:
        """Force a fresh investigation of one item, ignoring the cache.

        Evicts the cached resolution and re-runs the investigator with the
        currently-loaded context — regardless of pause state (it's an explicit
        user action). Use after a repo sync, or to reprocess a flagged item.
        """
        a = self.state.items.get(key)
        if a is None:
            return False, "no such item"
        self._cache.evict(key)
        a.resolution = Resolution(verdict=Verdict.PENDING)
        a.stale = False
        self.state.analysing_key = key
        self._status(f"re-evaluating {a.match.path}…")
        self._notify()
        try:
            resolution = await self._investigator.investigate(a.match)
            resolution.file_sig = _file_sig(a.match.abs_path)
            a.resolution = resolution
            self._cache.put(a)
            ok, msg = True, "re-evaluated"
        except Exception as e:  # noqa: BLE001
            a.resolution = Resolution(verdict=Verdict.ERROR, error=f"{type(e).__name__}: {e}")
            ok, msg = False, str(e)
        finally:
            self.state.analysing_key = None
            self._status("idle")
            self._notify()
        return ok, msg

    async def _discovery_cycle(self) -> None:
        # NL discovery is expensive; run it only when we have nothing cached or
        # the user forced a refresh. Otherwise keep the cached findings.
        self._drop_excluded()
        if self.state.items and not self._force_discovery:
            return
        if not self.state.analysis_enabled:
            return
        self._force_discovery = False
        self._status(f"discovering ({self.work.label})…")
        found = await self._investigator.discover(self.work)
        live_keys: set[str] = set()
        for m, r in found:
            r.file_sig = _file_sig(m.abs_path)
            a = AnalyzedMatch(m, r)
            live_keys.add(a.key)
            self.state.items[a.key] = a
            self._cache.put(a)
        # Prune previously-cached findings no longer reported.
        for gone in set(self.state.items) - live_keys:
            if self.state.items[gone].verdict != Verdict.APPLIED:
                del self.state.items[gone]
        self._cache.prune(live_keys)
        self._notify()

    async def _investigate_all(self, needs: list[Match]) -> None:
        total = len(needs)
        for i, m in enumerate(needs, 1):
            if not self.state.analysis_enabled:
                break
            self.state.analysing_key = m.key
            occ = f" ({m.occurrence_count} occurrences)" if m.occurrence_count > 1 else ""
            self._status(f"Copilot investigating {m.path}{occ} ({i}/{total})…")
            try:
                resolution = await self._investigator.investigate(m)
                resolution.file_sig = _file_sig(m.abs_path)
                a = AnalyzedMatch(m, resolution)
                self.state.items[m.key] = a
                self._cache.put(a)
            finally:
                self.state.analysing_key = None
            self._notify()

    async def run(self) -> None:
        async with self._investigator:
            while not self._stop.is_set():
                await self._scan_once()
                self._wake.clear()
                stop_t = asyncio.create_task(self._stop.wait())
                wake_t = asyncio.create_task(self._wake.wait())
                try:
                    await asyncio.wait(
                        {stop_t, wake_t},
                        timeout=self.interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for t in (stop_t, wake_t):
                        if not t.done():
                            t.cancel()


# --- git apply --------------------------------------------------------------

def _file_sig(abs_path: str) -> str:
    """Whole-file content hash — used to detect a file changing on disk (e.g.
    after a repo sync) even when a match's focus span is untouched."""
    try:
        with open(abs_path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:12]
    except OSError:
        return ""


def _git_apply(root: Path, diff: str) -> tuple[bool, str]:
    if not diff.strip():
        return False, "empty diff"
    body = diff if diff.endswith("\n") else diff + "\n"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".patch", delete=False, encoding="utf-8", newline="\n"
    ) as f:
        f.write(body)
        patch = f.name
    # `--recount` tolerates inaccurate @@ line counts from the model.
    attempts = [
        ["git", "apply", "--recount", patch],
        ["git", "apply", "--recount", "--ignore-whitespace", patch],
        ["git", "apply", "--recount", "--ignore-whitespace", "-C1", patch],
    ]
    last = ""
    try:
        for cmd in attempts:
            proc = subprocess.run(
                cmd, cwd=str(root), capture_output=True, text=True, timeout=30
            )
            if proc.returncode == 0:
                return True, "applied"
            last = (proc.stderr or proc.stdout).strip()
    except Exception as e:  # noqa: BLE001
        last = f"{type(e).__name__}: {e}"
    finally:
        try:
            Path(patch).unlink()
        except OSError:
            pass
    return False, last or "git apply failed"
