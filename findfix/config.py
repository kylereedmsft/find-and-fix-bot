"""Work configuration — the JSON-driven list of "units of work".

Each `WorkConfig` is one unit of work == one tab in the UI. Work is loaded
from a JSON file (default `findfix.config.json` in the current directory);
a built-in default set is used when no file is present.

A pattern can be a **regex** (deterministic scan), a natural-language
**description** (LLM discovery pass), or **both** (regex narrows, the
description guides the per-match investigation).

Everything downstream — scan scope, refiner, cache scope, MCP tool surface,
tab — derives from a `WorkConfig`, so adding a unit of work is a JSON edit,
never a code change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_NAME = "findfix.config.json"

# Directories we never descend into, on top of any per-work excludes.
_ALWAYS_EXCLUDE = [
    "**/.git/**",
    "**/.venv/**",
    "**/venv/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/.mypy_cache/**",
    "**/dist/**",
    "**/build/**",
]


@dataclass(slots=True, frozen=True)
class WorkConfig:
    label: str
    # --- what to scan -----------------------------------------------------
    root: str = "."                       # scan root (relative paths resolved from cwd)
    include: tuple[str, ...] = ("**/*",)  # glob(s) of files to scan
    exclude: tuple[str, ...] = ()         # extra glob(s) to skip
    # --- how to find ------------------------------------------------------
    regex: str | None = None              # deterministic pattern; None => description-only
    regex_flags: tuple[str, ...] = ()     # e.g. ("IGNORECASE", "MULTILINE")
    description: str | None = None        # natural-language pattern / intent
    context: str | None = None            # extra guidance for the investigator: gotchas,
                                          # preferred APIs, migration helpers, constraints
    max_matches: int = 200                # cap deterministic hits per cycle
    # --- how to focus the fix --------------------------------------------
    refiner: str = "line-window"          # line-window | treesitter | roslyn
    context_lines: int = 12               # window size for line-window refiner
    language: str | None = None           # hint for treesitter/roslyn (e.g. "python", "csharp")
    # --- how the LLM may investigate -------------------------------------
    # Extra read-only MCP servers granted to the investigator session, e.g.
    # ADO `search_code` or bluebird code intelligence. Passed through to the
    # Copilot SDK `mcp_servers` kwarg mostly verbatim (see investigator.py).
    mcp: dict = field(default_factory=dict)
    skill: str | None = None              # optional skill dir name under skills/

    # ---- derived ---------------------------------------------------------

    @property
    def root_path(self) -> Path:
        return Path(self.root).expanduser().resolve()

    @property
    def scope_key(self) -> str:
        """Cache scope: stable across runs, changes when the pattern changes."""
        h = hashlib.sha1(
            f"{self.root_path}|{self.regex}|{self.description}|{self.context}".encode("utf-8", "replace")
        ).hexdigest()[:10]
        return f"{self.label}:{h}"

    @property
    def all_excludes(self) -> tuple[str, ...]:
        return tuple(self.exclude) + tuple(_ALWAYS_EXCLUDE)

    @property
    def is_description_only(self) -> bool:
        return not self.regex and bool(self.description)


def _coerce(raw: dict) -> WorkConfig:
    if "label" not in raw:
        raise ValueError("each work item requires a 'label'")

    def tup(key: str, default=()):
        v = raw.get(key, default)
        if v is None:
            return ()
        if isinstance(v, str):
            return (v,)
        return tuple(v)

    def text(key: str):
        """Accept a plain string or a list of strings (joined with newlines)."""
        v = raw.get(key)
        if isinstance(v, (list, tuple)):
            return "\n".join(str(x) for x in v)
        return v

    return WorkConfig(
        label=str(raw["label"]),
        root=str(raw.get("root", ".")),
        include=tup("include", ("**/*",)) or ("**/*",),
        exclude=tup("exclude"),
        regex=raw.get("regex"),
        regex_flags=tup("regex_flags"),
        description=text("description"),
        context=text("context"),
        max_matches=int(raw.get("max_matches", 200)),
        refiner=str(raw.get("refiner", "line-window")),
        context_lines=int(raw.get("context_lines", 12)),
        language=raw.get("language"),
        mcp=dict(raw.get("mcp", {})),
        skill=raw.get("skill"),
    )


def load_work_configs(path: str | Path | None = None) -> list[WorkConfig]:
    """Load work units from JSON, or return the built-in defaults.

    Accepts either a top-level list, or an object with a "work" key.
    """
    p = Path(path) if path else Path.cwd() / DEFAULT_CONFIG_NAME
    if not p.exists():
        if path:  # explicitly requested but missing
            raise FileNotFoundError(f"config not found: {p}")
        return list(DEFAULT_WORK)
    doc = json.loads(p.read_text(encoding="utf-8"))
    items = doc.get("work", doc) if isinstance(doc, dict) else doc
    if not isinstance(items, list) or not items:
        raise ValueError(f"{p}: expected a non-empty list of work items")
    return [_coerce(x) for x in items]


# Built-in defaults so `python -m findfix` does something useful with no
# config file present. These scan this very repo.
DEFAULT_WORK: list[WorkConfig] = [
    WorkConfig(
        label="TODO/FIXME",
        root=".",
        include=("**/*.py",),
        regex=r"#\s*(TODO|FIXME|HACK|XXX)\b",
        description=(
            "Unresolved TODO/FIXME/HACK markers that represent real, "
            "actionable follow-up work in the code."
        ),
        refiner="treesitter",
        language="python",
    ),
    WorkConfig(
        label="bare-except",
        root=".",
        include=("**/*.py",),
        regex=r"except\s*:",
        description=(
            "Bare `except:` clauses that swallow all exceptions (including "
            "KeyboardInterrupt/SystemExit) and should catch a specific type."
        ),
        refiner="treesitter",
        language="python",
    ),
]
