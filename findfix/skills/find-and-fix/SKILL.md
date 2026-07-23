---
name: "find-and-fix"
description: "Investigate a candidate code pattern match and propose a minimal, correct fix as a unified diff. Use when triaging a regex/text hit or a natural-language-described code problem, confirming it is a real instance in context, and producing a surgical patch."
---

# Find and Fix

Turn a *candidate* pattern match into either a confident **skip** (false
positive / already fine) or a **minimal, correct fix** expressed as a unified
diff. You investigate one match at a time, read-only, and never edit files
yourself — the harness applies your proposed diff on the user's command.

## Method

1. **Understand the pattern.** You are given a regex and/or a natural-language
   description of the problem class, plus one concrete match (file, line, and a
   focused source span the harness selected — often the enclosing function or
   block).

2. **Confirm it's real, in context.** Use your read tools and any granted
   code-intelligence MCP tools (Azure DevOps `search_code`, bluebird) to check:
   - Is this actually an instance of the described problem, or a look-alike?
   - What calls this / what does it call? Would a change ripple?
   - Is the pattern systemic (many sites) or local?
   Prefer evidence over assumption. If the match is a false positive, `skip`
   with a one-line reason.

3. **Design the smallest safe fix.** Change only what the problem requires.
   Preserve behavior except for the defect. Match surrounding style. Don't
   reformat, rename, or "improve" unrelated code. If a correct fix needs
   information you can't get, `skip` and say what's missing.

4. **Emit a valid git-style unified diff.**
   - `--- a/<path>` / `+++ b/<path>` using the path relative to the repo root,
     exactly as given in the match.
   - Correct `@@` hunk(s) with real surrounding context lines.
   - Keep hunks tight; one file per set of headers.

## Severity / worth-fixing bar

Only propose a fix for a genuine, actionable defect or clear improvement that
the pattern is meant to catch. Stylistic nits, debatable preferences, and
speculative changes are `skip`. An all-`skip` result is a perfectly valid
outcome.

## Output

Respond with ONLY the fenced `json` block the system message specifies — a
`verdict`, `confidence`, `explanation`, and `diff`. No prose outside the fence.
