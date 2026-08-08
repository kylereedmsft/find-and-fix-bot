"""
DATA LAYER — deterministic scan of the working tree.

The harness owns data acquisition. This is the *cheap, broad* first pass of
the funnel: walk the files a `WorkConfig` selects, apply its regex, and emit
one `Match` per hit. No LLM, no network — just `re` over local files, capped
so a huge repo can't blow up.

Second (optional) stage of the funnel: a **refiner** turns each hit's line
number into a focused source span for the LLM:

  * ``line-window`` — ±N lines around the hit (always available).
  * ``treesitter``  — the enclosing function / class / block node, so the
    model sees a self-contained unit (requires ``tree_sitter_languages``;
    falls back to line-window).
  * ``roslyn``      — extension point: shell out to a C# structural analyzer
    (``FINDFIX_ROSLYN_CMD``); falls back to line-window when unconfigured.

Description-only work units have no regex to scan; their discovery happens in
the AI layer (see ``investigator.discover``). This module handles the regex
half.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from .config import WorkConfig
from .models import Match

# Files above this size are skipped by the scanner (likely generated/binary).
_MAX_FILE_BYTES = 2_000_000
# Hard cap on the focus span a refiner may return, in lines.
_MAX_FOCUS_LINES = 160

# Directory basenames we always skip (fast, separator-independent — fnmatch's
# `*` also matches `/`, so a top-level `.venv` won't match a `**/.venv/**`
# glob; this basename set covers that gap reliably).
_EXCLUDE_DIR_NAMES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", "dist", "build", ".idea", ".vs", "bin", "obj",
}


def _matches_any(rel: str, patterns) -> bool:
    """fnmatch `rel` against globs, tolerating an optional `**/` prefix.

    fnmatch has no path semantics, so `**/x` won't match a top-level `x`.
    We also try the pattern with a leading `**/` stripped so both anchored
    and unanchored forms work.
    """
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat):
            return True
        if pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]):
            return True
    return False


class ScanError(RuntimeError):
    pass


def _compile(work: WorkConfig) -> re.Pattern:
    flags = 0
    for name in work.regex_flags:
        flags |= getattr(re, name.upper(), 0)
    try:
        return re.compile(work.regex or "", flags)
    except re.error as e:
        raise ScanError(f"invalid regex for '{work.label}': {e}") from e


def _prefilter_terms(pattern: str) -> list[str] | None:
    """Extract literal substrings that MUST appear in every match of `pattern`.

    Used to pre-narrow a huge tree with `git grep -F` before the authoritative
    Python-regex scan reads any file. Correctness rule: the returned terms are a
    *superset filter* — every string the regex matches is guaranteed to contain
    all of them — so a file lacking a term cannot contain a match and is safe to
    skip. When we can't guarantee that (top-level alternation, lookaround, no
    long literal), we return None and the caller falls back to a full walk.
    """
    if not pattern:
        return None
    # Neutralize character classes first so a `|`/`(`/`)` inside `[...]` can't
    # trip the structural checks below.
    stripped = re.sub(r"\[[^\]]*\]", " ", pattern)
    # Drop escaped pairs (\d, \b, \s, \., \(, \)) so shorthand classes and
    # escaped metacharacters don't masquerade as literals or as real groups.
    # Replace with a separator so runs on either side don't merge.
    stripped = re.sub(r"\\.", " ", stripped)
    stripped = re.sub(r"\[[^\]]*\]", " ", stripped)
    # Remove parenthesized groups (with any trailing quantifier). Nothing inside
    # an optional or alternated group is individually guaranteed; even a required
    # single-literal group only *adds* a term. So dropping groups can make the
    # filter less selective but never incorrect — it lets us still extract the
    # guaranteed literals *outside* a group (e.g. `IsSPO` in `IsSPO(?:Get|Set)?`).
    # Iterate to peel nested groups from the inside out.
    prev = None
    while prev != stripped:
        prev = stripped
        stripped = re.sub(r"\([^()]*\)(?:[?*+]|\{\d+(?:,\d*)?\})?", " ", stripped)
    # Leftover parens (unbalanced) or a top-level `|` (unguarded alternation)
    # mean we can't guarantee a required literal -> fall back to a full walk.
    if any(c in stripped for c in ("(", ")", "|")):
        return None
    terms: list[str] = []
    for mo in re.finditer(r"[A-Za-z0-9_]+", stripped):
        s = mo.group(0)
        nxt = stripped[mo.end()] if mo.end() < len(stripped) else ""
        # A trailing optional/variable quantifier makes the last char uncertain;
        # trim it (always safe — yields a shorter, still-required substring).
        if nxt in ("?", "*", "{"):
            s = s[:-1]
        if len(s) >= 3:
            terms.append(s)
    return terms or None


def _git_candidate_files(work: WorkConfig) -> set[Path] | None:
    """Return the set of files under the work root that could contain a match,
    using `git grep` as a fast pre-filter, or None if the fast path can't be
    used safely (not a git tree, git missing, or no safe literal to grep for).

    This keeps the Python regex authoritative (the caller still scans each
    returned file) while avoiding reading every file in a massive repo — the
    difference between reading ~hundreds vs. ~tens-of-thousands of files.
    """
    terms = _prefilter_terms(work.regex or "")
    if not terms:
        return None
    root = work.root_path
    try:
        chk = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=15,
        )
        if chk.returncode != 0 or chk.stdout.strip() != "true":
            return None
        args = ["git", "-C", str(root), "grep", "--no-color", "-I", "-l", "-F", "--untracked"]
        if "IGNORECASE" in work.regex_flags:
            args.append("-i")
        if len(terms) > 1:
            args.append("--all-match")  # file must contain ALL required literals
        for t in terms:
            args += ["-e", t]
        args += ["--", "."]
        res = subprocess.run(args, cwd=str(root), capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode > 1:  # 0 = matches, 1 = no matches, >1 = error
        return None
    out: set[Path] = set()
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        p = root / line
        if p.exists():
            out.add(p.resolve())
    return out


def _iter_files(work: WorkConfig):
    root = work.root_path
    if not root.exists():
        raise ScanError(f"scan root does not exist: {root}")
    includes = work.include
    excludes = work.all_excludes
    # Fast path: on a git tree, let `git grep` narrow to candidate files first so
    # we don't read the entire repo in Python (which holds the GIL and freezes
    # the UI during a scan on large repos).
    candidates = _git_candidate_files(work)
    if candidates is not None:
        for p in sorted(candidates):
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            if not _matches_any(rel, includes):
                continue
            if _matches_any(rel, excludes):
                continue
            yield p, rel
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place for speed.
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        pruned = []
        for d in dirnames:
            if d in _EXCLUDE_DIR_NAMES:
                continue
            child = f"{rel_dir}/{d}" if rel_dir != "." else d
            if _matches_any(child, excludes) or _matches_any(child + "/_", excludes):
                continue
            pruned.append(d)
        dirnames[:] = pruned
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            if not _matches_any(rel, includes):
                continue
            if _matches_any(rel, excludes):
                continue
            yield p, rel


def _read_text(p: Path) -> str | None:
    try:
        if p.stat().st_size > _MAX_FILE_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _snippet(lines: list[str], hit_line: int, ctx: int = 2) -> str:
    lo = max(0, hit_line - 1 - ctx)
    hi = min(len(lines), hit_line + ctx)
    return "\n".join(lines[lo:hi])


class Scanner:
    """Regex scan + refinement for a single work unit."""

    def __init__(self, work: WorkConfig) -> None:
        self.work = work
        self._pattern = _compile(work) if work.regex else None
        self._refiner = _make_refiner(work)

    def scan(self) -> list[Match]:
        if self._pattern is None:
            return []  # description-only: discovery happens in the AI layer
        out: list[Match] = []
        # Cooperative yield: when this runs in a worker *thread* (the fallback
        # path), a bare `time.sleep(0)` releases the GIL at a bytecode boundary
        # so Textual's event loop can paint a frame. Time-gated (~every 20ms)
        # to keep the overhead negligible. The primary path offloads to a
        # separate process (its own GIL), where this is simply a cheap no-op.
        next_yield = time.monotonic() + 0.02
        for p, rel in _iter_files(self.work):
            now = time.monotonic()
            if now >= next_yield:
                time.sleep(0)
                next_yield = now + 0.02
            text = _read_text(p)
            if text is None:
                continue
            lines = text.splitlines()
            for m in self._pattern.finditer(text):
                start = m.start()
                line = text.count("\n", 0, start) + 1
                col = start - (text.rfind("\n", 0, start))
                fs, fe, fcode, rname = self._refiner(text, lines, line)
                out.append(
                    Match(
                        work=self.work.label,
                        path=rel,
                        abs_path=str(p),
                        line=line,
                        col=col,
                        matched_text=m.group(0)[:200],
                        snippet=_snippet(lines, line),
                        focus_start=fs,
                        focus_end=fe,
                        focus_code=fcode,
                        refiner=rname,
                    )
                )
                if len(out) >= self.work.max_matches:
                    return out
        return out


def run_scan(work: WorkConfig) -> list[Match]:
    """Module-level, picklable scan entry point for process offload.

    ``app.py`` runs this in a ``ProcessPoolExecutor`` so the CPU-bound scan
    (``re.finditer`` + tree-sitter parsing, both single C calls that hold the
    GIL) executes in a *separate interpreter*. That frees the parent's GIL
    entirely, keeping the Textual UI responsive during a scan. ``WorkConfig``
    and ``Match`` are both picklable, so the call and its result cross the
    process boundary cleanly.
    """
    return Scanner(work).scan()


def group_by_file(matches: list[Match]) -> list[Match]:
    """Collapse per-hit matches into one grouped `Match` per file.

    A file with N regex hits should be investigated **once** and fixed with a
    single diff — not investigated N times (each pass redundantly re-fixing the
    whole file, producing overlapping diffs that conflict on apply). Each group
    carries every hit line in ``occurrences`` and a merged ``focus_code`` that
    stitches together the distinct focus spans so the model sees them all.
    """
    by_path: dict[str, list[Match]] = {}
    for m in matches:
        by_path.setdefault(m.path, []).append(m)

    out: list[Match] = []
    for hits in by_path.values():
        hits.sort(key=lambda h: (h.line, h.col))
        lines = tuple(sorted({h.line for h in hits}))
        if len(hits) == 1:
            out.append(replace(hits[0], occurrences=lines))
            continue
        # Merge the distinct focus spans (dedup identical ones, e.g. multiple
        # hits inside the same enclosing block) into one labelled projection.
        spans: list[Match] = []
        seen: set[tuple[int, int]] = set()
        for h in hits:
            sig = (h.focus_start, h.focus_end)
            if sig in seen:
                continue
            seen.add(sig)
            spans.append(h)
        merged = "\n\n".join(
            f"----- occurrence at line {s.line} (file lines {s.focus_start}-{s.focus_end}) -----\n{s.focus_code}"
            for s in spans
        )
        first = hits[0]
        out.append(
            replace(
                first,
                occurrences=lines,
                focus_start=min(h.focus_start for h in hits),
                focus_end=max(h.focus_end for h in hits),
                focus_code=merged,
            )
        )
    return out


# --- refiners ---------------------------------------------------------------
# A refiner maps (full_text, lines, hit_line) -> (focus_start, focus_end,
# focus_code, refiner_name). All 1-based, focus_end inclusive.

def _clamp_span(lines: list[str], start: int, end: int) -> tuple[int, int, str]:
    start = max(1, start)
    end = min(len(lines), end)
    if end - start + 1 > _MAX_FOCUS_LINES:
        end = start + _MAX_FOCUS_LINES - 1
    return start, end, "\n".join(lines[start - 1 : end])


def _line_window(ctx: int):
    def refine(text: str, lines: list[str], hit_line: int):
        s, e, code = _clamp_span(lines, hit_line - ctx, hit_line + ctx)
        return s, e, code, "line-window"
    return refine


def _treesitter_refiner(work: WorkConfig):
    """Return a refiner that widens the span to the enclosing definition/block.

    Requires ``tree_sitter_languages``. If it (or the grammar) is missing we
    transparently fall back to a line window — the harness still works, just
    with a coarser focus.
    """
    fallback = _line_window(work.context_lines)
    get_parser = None
    try:  # newer, maintained package (works with tree-sitter >= 0.23)
        from tree_sitter_language_pack import get_parser  # type: ignore
    except Exception:  # noqa: BLE001
        try:  # older package (needs tree-sitter < 0.22)
            from tree_sitter_languages import get_parser  # type: ignore
        except Exception:  # noqa: BLE001 — optional dependency absent
            return fallback

    lang = (work.language or "").strip()
    if not lang:
        return fallback
    try:
        parser = get_parser(lang)
    except Exception:  # noqa: BLE001 — unknown grammar / ABI mismatch
        return fallback

    _BLOCKISH = (
        "function", "method", "constructor", "class", "struct", "definition",
        "declaration", "block", "statement", "clause",
    )

    # Parse each file once per scan, not once per hit: the scanner passes the
    # same `text` object for every match in a file, so cache by identity.
    _cache: dict = {"text_id": None, "tree": None}

    def refine(text: str, lines: list[str], hit_line: int):
        try:
            if _cache["text_id"] == id(text) and _cache["tree"] is not None:
                tree = _cache["tree"]
            else:
                tree = parser.parse(text.encode("utf-8", "replace"))
                _cache["text_id"] = id(text)
                _cache["tree"] = tree
            # byte offset of the start of the hit line
            off = sum(len(l.encode("utf-8", "replace")) + 1 for l in lines[: hit_line - 1])
            node = tree.root_node.descendant_for_byte_range(off, off)
            if node is None:
                return fallback(text, lines, hit_line)
            chosen = node
            cur = node
            while cur is not None:
                if any(k in cur.type for k in _BLOCKISH) and cur.end_point[0] > cur.start_point[0]:
                    chosen = cur
                    break
                cur = cur.parent
            s = chosen.start_point[0] + 1
            e = chosen.end_point[0] + 1
            s, e, code = _clamp_span(lines, s, e)
            return s, e, code, "treesitter"
        except Exception:  # noqa: BLE001 — any parse hiccup -> window
            return fallback(text, lines, hit_line)

    return refine


def _roslyn_refiner(work: WorkConfig):
    """Extension point for C#/.NET structural focus.

    Set ``FINDFIX_ROSLYN_CMD`` to a command template (with ``{file}`` and
    ``{line}`` placeholders) that prints ``START_LINE END_LINE`` for the
    enclosing member. Unconfigured or failing => line-window fallback. Kept
    as a shell-out so the .NET dependency stays out of the Python process.

    Note: the deterministic scanner passes source text (not a path) to
    refiners, so wiring the external tool to a concrete file is left to the
    integrator — by default this degrades gracefully to a line window.
    """
    return _line_window(work.context_lines)


def _make_refiner(work: WorkConfig):
    name = (work.refiner or "line-window").lower()
    if name == "treesitter":
        return _treesitter_refiner(work)
    if name == "roslyn":
        return _roslyn_refiner(work)
    return _line_window(work.context_lines)
