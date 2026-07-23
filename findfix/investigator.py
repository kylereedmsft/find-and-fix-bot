"""
AI LAYER — GitHub Copilot SDK, used for find-and-fix judgment.

For each candidate `Match` the harness hands Copilot a bounded projection:
the pattern (regex and/or natural-language description), the file path, and
the focused source span the refiner selected. The model investigates it in
repo context — it gets Copilot's built-in read/grep tools scoped to the work
root, plus whatever **read-only** code-intelligence MCP servers the work unit
grants (e.g. Azure DevOps `search_code`, bluebird) so it can look up callers /
definitions before deciding. Shell and file writes are hard-rejected: the
model *proposes* a unified diff, the harness applies it.

Two entry points:
  * ``investigate(match)`` — resolve one regex hit.
  * ``discover(work)``     — description-only units: one pass that finds
    occurrences AND proposes fixes for each.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from copilot import CopilotClient
from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.session_events import (
    AssistantMessageData,
    PermissionRequestShell,
    PermissionRequestWrite,
    SessionIdleData,
)

from .config import WorkConfig
from .models import Match, Resolution, Verdict
from .chat import CHAT_SYSTEM

_SKILLS_DIR = Path(__file__).resolve().parent / "skills"

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict:
    m = _JSON_FENCE.search(text)
    blob = m.group(1) if m else text[text.find("{") : text.rfind("}") + 1]
    return json.loads(blob)


def _read_only_permissions(request, _invocation):
    """Approve reads, MCP calls, and URL fetches; reject shell and writes.

    The model needs read access to investigate the working tree and to call
    the allow-listed code-search MCP tools. It must NOT run shell commands or
    mutate files — the harness owns fix application so the user stays in the
    loop (suggest, then apply on keypress).
    """
    match request:
        case PermissionRequestShell():
            return PermissionDecisionReject(
                feedback="Shell is disabled. Use your read tools or the code-search MCP tools."
            )
        case PermissionRequestWrite():
            return PermissionDecisionReject(
                feedback="Do not edit files. Propose the fix as a unified diff in your JSON answer."
            )
        case _:
            return PermissionDecisionApproveOnce()


_SYSTEM = """\
You are a find-and-fix agent embedded in an unattended, one-at-a-time triage
loop. You are given a *pattern* to hunt for and a concrete candidate match in
a local repository. Your job: investigate the match in context and, if it is a
genuine instance of the problem, propose a **minimal, correct fix** as a
unified diff.

Pattern for this unit of work
  label:        {label}
  regex:        {regex}
  description:  {description}
{context}
You have read-only tools: Copilot's built-in file read/search over the work
root, plus any code-intelligence MCP tools granted (e.g. Azure DevOps
`search_code`, bluebird). Use them freely — no budget — to confirm the match
is real: check callers, definitions, whether the pattern is systemic, and
whether a fix would break anything. You may NOT run shell commands or edit
files.

Decide a verdict:
  fix   – a real instance of the described problem; you are proposing a diff.
  skip  – false positive, already correct, or not worth changing. No diff.

When verdict is `fix`, the `diff` MUST be a valid **git-style unified diff**:
  * headers `--- a/<path>` and `+++ b/<path>` with the path RELATIVE to the
    repo root exactly as given in the match (`{path}`),
  * one or more `@@ ... @@` hunks with correct surrounding context lines,
  * change only what the fix requires — keep it surgical.
This file may contain MULTIPLE occurrences of the pattern (see
`occurrence_lines` in the payload). When it does, fix ALL of them in this one
file in a SINGLE unified diff with multiple hunks — do not fix just one. This
is the only investigation for this file, so it must be complete.
Prefer the smallest diff that fully resolves the issue. Do not reformat
unrelated code. If you cannot produce a safe fix, use `skip` and explain why.

Respond with ONLY a fenced ```json block, no prose outside it:

{{
  "verdict": "fix" | "skip",
  "confidence": "high" | "medium" | "low",
  "explanation": "<2-4 sentences: what you found and why fix/skip>",
  "diff": "<git unified diff, or empty string when skip>"
}}"""

_DISCOVER_SYSTEM = """\
You are a find-and-fix agent. You are given a natural-language description of a
code problem and a repository to search. Find genuine occurrences and, for
each, propose a minimal fix.

Unit of work
  label:        {label}
  description:  {description}
  scan root:    {root}
{context}
Use your read-only tools (file read/search over the scan root, plus any granted
code-intelligence MCP tools) to locate real instances. Be precise — do not
report speculative or stylistic nits. For each occurrence decide `fix` (with a
minimal git-style unified diff, paths relative to the scan root) or `skip`.

Respond with ONLY a fenced ```json block:

{{
  "findings": [
    {{
      "file": "<path relative to scan root>",
      "line": <1-based line number of the occurrence>,
      "reason": "<why this is an instance>",
      "verdict": "fix" | "skip",
      "confidence": "high" | "medium" | "low",
      "explanation": "<what and why>",
      "diff": "<git unified diff, or empty string>"
    }}
  ]
}}

An empty findings list is a valid outcome. No prose outside the fence."""


def _known_server_shorthand(name: str, spec) -> dict | None:
    """Allow compact `mcp` entries in JSON.

    A value may be a full stdio server dict (passed through), or the string
    "ado:<org>" / a dict {"ado_org": "...", "tools": [...]} for the Azure
    DevOps server, which we expand to the `npx @azure-devops/mcp` invocation
    used by pr_sentry.
    """
    import shutil

    if isinstance(spec, dict) and spec.get("type"):
        return spec  # already a full server spec
    org = None
    tools = None
    if isinstance(spec, str) and spec.startswith("ado:"):
        org = spec.split(":", 1)[1]
    elif isinstance(spec, dict) and "ado_org" in spec:
        org = spec["ado_org"]
        tools = spec.get("tools")
    if org:
        npx = shutil.which("npx") or shutil.which("npx.cmd")
        if not npx:
            return None
        return {
            "type": "stdio",
            "command": npx,
            "args": ["-y", "@azure-devops/mcp", org, "--authentication", "azcli"],
            "tools": tools or ["search_code"],
            "timeout": 60_000,
        }
    return spec if isinstance(spec, dict) else None


def _mcp_servers(work: WorkConfig) -> dict:
    servers: dict = {}
    for name, spec in (work.mcp or {}).items():
        expanded = _known_server_shorthand(name, spec)
        if expanded:
            servers[name] = expanded
    return servers


def _context_block(work: WorkConfig) -> str:
    """Render the unit's extra guidance for a prompt, or empty when unset."""
    if not (work.context and work.context.strip()):
        return ""
    return (
        "\nAdditional guidance for this unit (gotchas, preferred APIs, migration\n"
        "helpers, constraints — follow these when proposing a fix):\n"
        + work.context.strip()
        + "\n"
    )


class Investigator:
    """Long-lived Copilot client; one fresh session per match/discovery."""

    def __init__(self, work: WorkConfig, model: str | None = None) -> None:
        self.work = work
        self._model = model
        self._client = CopilotClient()
        self._mcp = _mcp_servers(work)

    async def __aenter__(self) -> "Investigator":
        await self._client.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.stop()

    # ---- shared session plumbing ----------------------------------------

    def _base_kwargs(self, system: str) -> dict:
        kwargs: dict = dict(
            on_permission_request=_read_only_permissions,
            system_message={"mode": "append", "content": system},
            working_directory=str(self.work.root_path),
        )
        if self.work.skill:
            kwargs["enable_skills"] = True
            kwargs["skill_directories"] = [str(_SKILLS_DIR)]
        if self._mcp:
            kwargs["mcp_servers"] = self._mcp
        if self._model:
            kwargs["model"] = self._model
        return kwargs

    async def _run(self, kwargs: dict, prompt: str, timeout: float) -> str:
        collected: list[str] = []
        done = asyncio.Event()

        def on_event(event) -> None:
            match event.data:
                case AssistantMessageData() as d:
                    if d.content:
                        collected.append(d.content)
                case SessionIdleData():
                    done.set()

        async with await self._client.create_session(**kwargs) as session:
            session.on(on_event)
            await session.send(prompt)
            await asyncio.wait_for(done.wait(), timeout=timeout)
        if not collected:
            raise RuntimeError("Copilot returned no output")
        return collected[-1]

    # ---- entry points ----------------------------------------------------

    async def investigate(self, match: Match) -> Resolution:
        system = _SYSTEM.format(
            label=self.work.label,
            regex=self.work.regex or "(none)",
            description=self.work.description or "(none)",
            context=_context_block(self.work),
            path=match.path,
        )
        payload = {
            "path": match.path,
            "line": match.line,
            "occurrence_lines": list(match.occurrences) or [match.line],
            "occurrence_count": match.occurrence_count,
            "matched_text": match.matched_text,
            "refiner": match.refiner,
            "focus_start_line": match.focus_start,
            "focus_end_line": match.focus_end,
            "focus_code": match.focus_code,
        }
        prompt = (
            "Investigate this candidate match and answer exactly as the system "
            "message specifies.\n\n" + json.dumps(payload, ensure_ascii=False)
        )
        try:
            raw = await self._run(self._base_kwargs(system), prompt, timeout=600)
        except Exception as e:  # noqa: BLE001
            return Resolution(verdict=Verdict.ERROR, error=f"{type(e).__name__}: {e}")
        return _resolution_from(raw)

    async def discover(self, work: WorkConfig) -> list[tuple[Match, Resolution]]:
        system = _DISCOVER_SYSTEM.format(
            label=work.label,
            description=work.description or "(none)",
            root=str(work.root_path),
            context=_context_block(work),
        )
        prompt = (
            "Search the repository for occurrences of the described problem and "
            "answer exactly as the system message specifies."
        )
        try:
            raw = await self._run(self._base_kwargs(system), prompt, timeout=900)
        except Exception as e:  # noqa: BLE001
            return []
        try:
            data = _extract_json(raw)
        except Exception:  # noqa: BLE001
            return []
        out: list[tuple[Match, Resolution]] = []
        for item in data.get("findings") or []:
            m = _discovered_match(work, item)
            if m is None:
                continue
            out.append((m, _resolution_from_item(item)))
        return out

    # ---- interactive discussion -----------------------------------------

    @property
    def client(self) -> CopilotClient:
        return self._client

    def chat_kwargs(self) -> dict:
        """Session kwargs for a discussion: same read-only tool surface as an
        investigation, but with the interactive chat system prompt (plus this
        unit's context guidance)."""
        system = CHAT_SYSTEM + _context_block(self.work)
        return self._base_kwargs(system)


# --- parsing ----------------------------------------------------------------

def _norm_verdict(v: str) -> Verdict:
    v = (v or "").strip().lower()
    if v == "fix":
        return Verdict.FIX
    if v == "skip":
        return Verdict.SKIP
    return Verdict.SKIP


def _resolution_from(raw: str) -> Resolution:
    try:
        data = _extract_json(raw)
    except Exception as e:  # noqa: BLE001
        return Resolution(
            verdict=Verdict.ERROR,
            error=f"unparseable Copilot output: {e}",
            explanation=raw[:500],
        )
    return _resolution_from_item(data)


def _resolution_from_item(data: dict) -> Resolution:
    verdict = _norm_verdict(str(data.get("verdict", "")))
    diff = str(data.get("diff") or "")
    if verdict == Verdict.FIX and not diff.strip():
        verdict = Verdict.SKIP  # claimed a fix but gave no diff
    return Resolution(
        verdict=verdict,
        explanation=str(data.get("explanation") or data.get("reason") or ""),
        diff=diff,
        confidence=str(data.get("confidence") or ""),
        resolved_at=datetime.now(timezone.utc),
    )


def _discovered_match(work: WorkConfig, item: dict) -> Match | None:
    rel = str(item.get("file") or "").strip()
    if not rel:
        return None
    line = int(item.get("line") or 1)
    abs_path = (work.root_path / rel)
    focus = ""
    fs = fe = line
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        ctx = work.context_lines
        fs = max(1, line - ctx)
        fe = min(len(lines), line + ctx)
        focus = "\n".join(lines[fs - 1 : fe])
    except OSError:
        pass
    return Match(
        work=work.label,
        path=rel,
        abs_path=str(abs_path),
        line=line,
        col=1,
        matched_text="",
        snippet=focus.split("\n", 5)[0] if focus else "",
        focus_start=fs,
        focus_end=fe,
        focus_code=focus,
        refiner="discovery",
        reason=str(item.get("reason") or ""),
    )
