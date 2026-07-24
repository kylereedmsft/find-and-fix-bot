"""
Azure DevOps work-item creation — harness-owned, deterministic, no LLM.

Filing a work item is an *action the tool takes on the user's behalf*, so it
lives here rather than behind the read-only investigator model. We shell out to
the `az` CLI (`az boards work-item create`), reusing the same `azcli`
authentication the ADO MCP server uses — so if the user can search ADO from the
investigator, they can file items too, with no extra credentials.

Granularity is **one work item per file**: the title/description cover every
occurrence in the match's file. Idempotency lives one layer up — `app.py`
records the returned id in `Resolution.work_item_id` and refuses to re-file.

`az` may be missing or logged out in a given environment; every entry point
returns a `(ok, ...)` tuple with a clear message instead of raising, so the TUI
can surface it like any other action error.
"""

from __future__ import annotations

import html
import json
import shutil
import subprocess
from dataclasses import dataclass

from .config import AdoTracking
from .models import AnalyzedMatch


@dataclass(slots=True)
class WorkItemResult:
    ok: bool
    work_item_id: int | None = None
    url: str = ""
    message: str = ""


def _az() -> str | None:
    return shutil.which("az") or shutil.which("az.cmd")


def build_title(tracking: AdoTracking, work_label: str, a: AnalyzedMatch) -> str:
    """One-line summary. Honors `title_template` with {label}/{path}/{line}."""
    tmpl = tracking.title_template or "[{label}] {path}"
    try:
        title = tmpl.format(label=work_label, path=a.match.path, line=a.match.line)
    except (KeyError, IndexError):
        title = f"[{work_label}] {a.match.path}"
    return title[:255]  # ADO System.Title cap


def build_description(work_label: str, a: AnalyzedMatch) -> str:
    """HTML body for System.Description: intent, occurrences, and the diff.

    ADO renders System.Description as HTML, so we emit escaped HTML with a
    <pre> block for the proposed diff.
    """
    m, r = a.match, a.resolution
    parts: list[str] = []
    parts.append(f"<p><b>Work unit:</b> {html.escape(work_label)}</p>")
    parts.append(
        f"<p><b>File:</b> {html.escape(m.path)}<br/>"
        f"<b>Refiner:</b> {html.escape(m.refiner)}</p>"
    )
    if m.occurrence_count > 1:
        lines = ", ".join(str(x) for x in m.occurrences)
        parts.append(
            f"<p><b>{m.occurrence_count} occurrences</b> "
            f"(fixed together): lines {html.escape(lines)}</p>"
        )
    else:
        parts.append(f"<p><b>Occurrence:</b> line {m.line}</p>")
    if r.explanation:
        parts.append(f"<p><b>Explanation:</b> {html.escape(r.explanation)}</p>")
    if r.diff.strip():
        parts.append("<p><b>Proposed fix:</b></p>")
        parts.append(f"<pre>{html.escape(r.diff)}</pre>")
    parts.append(
        "<p><i>Filed by find-and-fix-bot.</i></p>"
    )
    return "".join(parts)


def create_work_item(
    tracking: AdoTracking, title: str, description: str
) -> WorkItemResult:
    """Create a work item via `az boards work-item create`.

    Returns a WorkItemResult; never raises. On success, `work_item_id`/`url`
    are populated. On failure, `message` explains why (missing az, logged out,
    missing extension, ADO error, etc.).
    """
    az = _az()
    if not az:
        return WorkItemResult(
            ok=False,
            message="`az` CLI not found — install Azure CLI to file work items.",
        )
    org_url = tracking.org
    if not org_url.startswith("http"):
        org_url = f"https://dev.azure.com/{org_url}"

    cmd = [
        az, "boards", "work-item", "create",
        "--org", org_url,
        "--project", tracking.project,
        "--type", tracking.type,
        "--title", title,
        "--description", description,
        "--output", "json",
    ]
    if tracking.area_path:
        cmd += ["--area", tracking.area_path]
    if tracking.iteration_path:
        cmd += ["--iteration", tracking.iteration_path]
    fields: list[str] = []
    if tracking.tags:
        fields.append(f"System.Tags={';'.join(tracking.tags)}")
    if fields:
        cmd += ["--fields", *fields]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return WorkItemResult(ok=False, message=f"{type(e).__name__}: {e}")

    if proc.returncode != 0:
        return WorkItemResult(ok=False, message=_friendly_error(proc.stderr or proc.stdout))

    try:
        data = json.loads(proc.stdout)
        wid = int(data["id"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return WorkItemResult(ok=False, message="could not parse az output for the new work item id")

    url = _web_url(org_url, tracking.project, wid)
    # Best-effort parent link; a failure here doesn't undo the created item.
    if tracking.parent is not None:
        _link_parent(az, org_url, wid, tracking.parent)
    return WorkItemResult(ok=True, work_item_id=wid, url=url, message=f"created #{wid}")


def _link_parent(az: str, org_url: str, child_id: int, parent_id: int) -> None:
    cmd = [
        az, "boards", "work-item", "relation", "add",
        "--org", org_url,
        "--id", str(child_id),
        "--relation-type", "parent",
        "--target-id", str(parent_id),
        "--output", "none",
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:  # noqa: BLE001 — link is best-effort
        pass


def _web_url(org_url: str, project: str, wid: int) -> str:
    return f"{org_url.rstrip('/')}/{project}/_workitems/edit/{wid}"


def _friendly_error(raw: str) -> str:
    text = (raw or "").strip()
    low = text.lower()
    if "az login" in low or "not logged in" in low or "no cached token" in low \
            or "sign in" in low or "az account" in low:
        return "not logged in to Azure — run `az login` first."
    if "is not in the 'az' command group" in low or "boards" in low and "not" in low \
            and "found" in low:
        return "`az boards` unavailable — run `az extension add --name azure-devops`."
    return text[:400] or "az boards work-item create failed"
