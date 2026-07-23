# find-and-fix-bot — a Copilot SDK "standalone harness"

Continuously scans a **local repo / working tree** for configurable
*patterns*. When a pattern hits, it **investigates the code in context** and
**proposes a fix** as a unified diff that you can **apply from the TUI** with a
keypress. Each *unit of work* is a JSON entry and gets its own tab.

It's a reference for a pattern: **building a purpose-built tool around the
Copilot SDK** instead of prompting a general agent to "go fix stuff". Derived
from the `SampleHarness/pr_sentry` exemplar.

```
 find-and-fix   copilot-sdk   ~ Copilot investigating app.py:142 (2/5)…
 ● TODO/FIXME   ● bare-except   ● sync-file-io     12 match · 3 fix · next 41s
┌──────────────────────── TODO/FIXME — 12 match(es) ─────────────────────────┐
│  Status         File               Line  Match / reason            Fix     │
│  FIX            findfix/app.py      142   # TODO: handle retry      diff    │
│  / investigating findfix/tui.py     88    # FIXME: flicker          -       │
│  applied        findfix/cache.py    30    # TODO: prune             applied │
│  skip           findfix/scanner.py  17    # XXX: perf               -       │
└─────────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────── Details ───────────────────────────────────┐
│  F  findfix/app.py:142   [TODO/FIXME]   confidence: high                    │
│  focus: lines 130-158 via treesitter                                        │
│  Explanation — The retry TODO is a real gap: transient failures aren't …    │
│  Proposed fix:                                                              │
│   --- a/findfix/app.py                                                      │
│   +++ b/findfix/app.py                                                      │
│   @@ ...                                                                    │
│  Press 'a' to apply this fix.                                              │
└─────────────────────────────────────────────────────────────────────────────┘
 ← Prev  → Next  SPACE Start/pause  a Apply fix  e Re-eval  d Discuss  r Refresh  o Open  q Quit
```

## The funnel

Cheap and broad first, expensive and precise last:

1. **Text search / regex** (`scanner.py`) — walk the files a unit selects,
   apply its regex. No LLM, no network. This *narrows* the repo to candidate
   sites fast.
2. **Structural refine** (optional) — turn each hit's line into a focused
   source span so the model sees a self-contained unit:
   * `line-window` — ±N lines (always available),
   * `treesitter` — the enclosing function/class/block (needs the
     `treesitter` extra; falls back to a window),
   * `roslyn` — extension point for C#/.NET (`FINDFIX_ROSLYN_CMD`).
3. **Investigate + fix** (`investigator.py`) — Copilot reads the focused span,
   uses read-only tools (its file search over the work root **plus** any
   granted code-intelligence MCP servers — Azure DevOps `search_code`,
   bluebird) to confirm the issue and propose a **minimal unified diff**. It
   never edits files; the harness applies the diff via `git apply` when you
   press `a`.

Natural-language-only units (no regex) skip step 1: the LLM does a discovery
pass that finds occurrences *and* proposes fixes in one shot.

## Architecture

| Layer | Module | What it does | Uses AI? |
|---|---|---|---|
| **Config** | `config.py` | `WorkConfig` + JSON loader. Each entry = one tab; derives scan scope, refiner, cache scope, MCP surface. | ❌ |
| **Data** | `scanner.py` | Regex/text scan → `Match[]`; pluggable refiner for the focus span. | ❌ Deterministic |
| **AI** | `investigator.py` + `skills/find-and-fix/` | Copilot session, read-only perms, granted code-search MCP tools. Confirms the match and emits a unified diff. | ✅ Judgment only |
| **Orchestration** | `app.py` + `cache.py` | Per-unit worker: scan → seed → investigate one-at-a-time; cache by content hash; `apply_fix` via `git apply`. | ❌ |
| **Presentation** | `tui.py` | Textual: tab per unit, match table, explanation + diff detail, `a` to apply. | ❌ |

## Configure work (JSON)

Work is declared in `findfix.config.json` (or `--config <path>`). See
`findfix.config.example.json` (Python). You can point a unit at any language —
e.g. a C# unit that flags a deprecated type and proposes the modern replacement.
A unit can be regex-only, description-only, or both.

```json
{
  "work": [
    {
      "label": "bare-except",
      "root": ".",
      "include": ["**/*.py"],
      "regex": "except\\s*:",
      "description": "Bare except: that swallows all exceptions; catch a specific type.",
      "refiner": "treesitter",
      "language": "python",
      "mcp": { "ado": { "ado_org": "your-ado-org", "tools": ["search_code"] } }
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `label` | Tab name + cache scope. |
| `root` | Scan root (default `.`). |
| `include` / `exclude` | Globs of files to scan / skip. |
| `regex` / `regex_flags` | Deterministic pattern (omit for description-only). |
| `description` | Natural-language pattern / intent (guides investigation; drives discovery when there's no regex). |
| `context` | Extra guidance injected into the investigator + discuss prompts: gotchas, preferred APIs, migration helpers, hard constraints. Accepts a string **or a list of strings** (joined with newlines — handy for readable multi-rule guidance). Also part of the cache scope, so editing it re-investigates. |
| `refiner` | `line-window` \| `treesitter` \| `roslyn`. |
| `context_lines` / `language` | Window size / grammar hint for the refiner. |
| `mcp` | Read-only MCP servers granted to the investigator. `{"name": {"ado_org": "org", "tools": [...]}}` expands to the Azure DevOps server; a full stdio spec is passed through verbatim. |
| `skill` | Optional skill dir under `skills/`. |
| `max_matches` | Cap on deterministic hits per cycle. |

No config file present → a built-in default set (TODO/FIXME, bare-except)
scanning this repo.

### Three ways to define a pattern

A `regex` (fast, deterministic) and a `description` (LLM judgment) can be used
independently or together:

```json
{ "label": "regex-only",   "include": ["**/*.py"], "regex": "except\\s*:" }
```
Deterministic scan finds every hit; the LLM then confirms each and proposes a
fix (the raw regex alone can't tell a real bug from a look-alike).

```json
{ "label": "description-only", "root": "src",
  "description": "Blocking file/network I/O inside an async function." }
```
No regex → the LLM does a discovery pass that finds occurrences **and** fixes
them in one shot. Best for conceptual patterns a regex can't express.

```json
{ "label": "both", "include": ["**/*.cs"], "regex": "AesManaged",
  "description": "Legacy AesManaged usage that should move to Aes.Create().",
  "refiner": "roslyn", "language": "csharp" }
```
Regex narrows the candidate set cheaply; the description guides the per-match
investigation. This is the recommended shape for most rules.

The `mcp` block grants the investigator read-only code intelligence. Shorthand
`{"ado": {"ado_org": "your-ado-org", "tools": ["search_code"]}}` expands to the
Azure DevOps MCP server (auth via `az`); any full stdio server spec (e.g. a
bluebird server) is passed through verbatim.

## Prerequisites

* **Python ≥ 3.11** (use a venv).
* **GitHub Copilot CLI** — installed and authenticated (`copilot` on PATH).
* **git** — on PATH (used to apply fixes).
* **Azure CLI** (`az login`) and **Node/npx** — only if a unit grants the
  Azure DevOps `search_code` MCP tool.

## Install & run

```powershell
cd C:\git\find-and-fix-bot
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[treesitter]"

.\.venv\Scripts\python -m findfix                     # interactive TUI
.\.venv\Scripts\python -m findfix --work bare-except  # one unit
.\.venv\Scripts\python -m findfix --interval 120
.\.venv\Scripts\python -m findfix --once              # one pass, print, exit
```

**Keys:** `←`/`→` switch unit · `↑`/`↓` select match · `SPACE` pause/resume ·
`a` apply the selected fix · `e` re-evaluate the selected match (`E` = whole
tab) · `d` discuss the selected match · `r` refresh (re-runs NL discovery) · `o`
open the file · `t` theme · `q` quit.

## Discuss & refine a fix (`d`)

Press `d` on any match to open an interactive chat about it. The model is
preloaded with the pattern, the focused code, the current proposed fix, and the
unit's `context` guidance — with the same read-only tools as the investigator
(file read/grep + any granted code-intelligence MCP), so it can look things up
live and cite `file:line`.

Give it feedback ("use the typed `GetOrAdd<T>` extension instead of a cast",
"make sure all access to a given key uses the same object", "gate it behind the
kill switch"). When you ask for a change, the model emits a revised fix that the
**harness** applies to the proposal (surfaced as a `[harness]` line); press `a`
in the table to apply the refined diff. `Esc` closes the chat but the session
lives on — press `d` again to resume.

**Persistence.** Each discussion transcript is written to
`%LOCALAPPDATA%\find-and-fix-bot\chats\<scope>\<match>.json`, and refined fixes
are saved to the resolution cache. Nothing is regenerated from scratch on
restart: prior resolutions and conversations are restored, and a re-scan won't
clobber a fix you refined.

## State & persistence

Everything expensive is cached on disk under `%LOCALAPPDATA%\find-and-fix-bot\`
and reloaded on launch, so restarting costs no LLM calls for unchanged code:

| What | Where | Notes |
|---|---|---|
| Resolutions (verdict, explanation, diff, confidence, `APPLIED`) | `resolutions.json` | Keyed by `(scope_key, match.key)`. `scope_key` = hash of `root\|regex\|description\|context`; `match.key` embeds a **content hash** of the focus span. |
| Discuss transcripts | `chats\<scope>\<match>.json` | Replayed into a fresh session as "PRIOR CONVERSATION" on restart. |
| Applied code changes | your git working tree | The actual fix — review/commit it like any edit. |

**Rebuilt each run (not persisted):** the live Copilot sessions (only transcript
*text* survives), the focus code (re-read from disk), and UI state (selection,
pause, status). The scan itself re-runs every launch — it's cheap and just
reuses cached judgments.

**Good to know:**
- Edit the code around a match and its content hash changes, so it's treated as
  new and re-investigated. Same for changing a unit's `regex`/`description`/
  `context` — that changes `scope_key`, so the unit re-investigates from scratch
  (the old cache slice is orphaned, not deleted).
- Matches that disappear from a scan are pruned from the cache. So once you apply
  a fix and the pattern no longer matches, that entry drops out of the cache on
  the next scan — it stays visible for the current session, and the change is
  safe in git, but the "applied" breadcrumb won't be restored next launch.
- Delete `resolutions.json` (or the whole folder) to force a clean re-scan.

### Re-evaluating & staleness (repo syncs)

The cache does **not** track a commit id. Each resolution snapshots a whole-file
hash (`file_sig`) at investigation time. On every scan the harness re-hashes each
matched file and, if it changed since the resolution, flags the item **`⟳ stale`**
(shown in the table and detail). This is what catches a `git pull`:

- A sync that changes a match's **focus span** already produces a new `match.key`,
  so it's re-investigated automatically.
- A sync that changes the file **elsewhere** (outside every focus window) leaves
  the key intact — the resolution would otherwise be served stale, so it's
  flagged instead.

Press **`e`** to force a fresh investigation of the selected match (or **`E`** for
the whole tab). Re-evaluation ignores the cache, re-runs with the currently
loaded `context`, and works even while analysis is paused. Note: to pick up an
edited `context` from the JSON you still relaunch (it changes `scope_key`, which
re-runs the whole unit) — `e`/`E` re-run against the context loaded in the
current session.

## Tests

The deterministic layers (config, scanner + refiners, cache, orchestration,
`git apply`) are covered by pytest. Tests **stub the Copilot investigator**, so
they need no model auth or network — the harness owns everything except the
judgment call, and that seam is mocked.

```powershell
.\.venv\Scripts\python -m pip install -e ".[treesitter,dev]"
.\.venv\Scripts\python -m pytest
```

| File | Covers |
|---|---|
| `tests/test_config.py` | JSON loading, defaults, coercion, `is_description_only`, scope keys |
| `tests/test_scanner.py` | regex scan, dir/glob excludes, `max_matches`, line-window + treesitter refiners, content-hash keys |
| `tests/test_cache_models.py` | cache round-trip / prune / scoping, verdict ordering, `has_fix` |
| `tests/test_app.py` | seed→investigate flow, cache reuse, pause behavior, `git apply` (success, `--recount`, non-applicable), force re-evaluate (cache-ignoring, works while paused), file-change staleness, `file_sig` persistence |
| `tests/test_chat.py` | per-unit `context` (scope key + prompt injection), discuss transcript persistence/restore, one-time context preload, revised-fix parsing → resolution update |

Each test runs against an isolated cache dir (`tests/conftest.py`), so your
real `%LOCALAPPDATA%` store is never touched.

## Adapting it

* **Different problems?** Edit `findfix.config.json`. Each unit is independent
  — its own worker, cache scope, refiner, MCP surface, tab.
* **Different structural analysis?** Add a refiner in `scanner.py`
  (`_make_refiner`) — e.g. wire the Roslyn shell-out.
* **Different fix delivery?** `FindFixApp.apply_fix` uses `git apply`; swap it
  for a branch+PR flow.
* **Different judgment?** Edit the system prompts in `investigator.py` and the
  `skills/find-and-fix/` skill.

## Files

```
findfix/
├── __main__.py     argparse → TUI or --once; --config / --work filters
├── config.py       WorkConfig, JSON loader, built-in defaults
├── models.py       Match, Resolution, Verdict, AnalyzedMatch, AppState
├── scanner.py      DATA:  regex scan + pluggable refiner (line/treesitter/roslyn)
├── investigator.py AI:    Copilot session (read-only perms + code-search MCP) → unified diff
├── app.py          ORCH:  per-unit scan loop, cache, git-apply fixes
├── cache.py        ORCH:  persistent {match.key → resolution} cache
├── tui.py          VIEW:  Textual tabs / match table / diff detail / apply
└── skills/
    └── find-and-fix/SKILL.md
tests/              pytest suite (stubs the investigator — no auth needed)
findfix.config.example.json   sample work config (Python)
```
