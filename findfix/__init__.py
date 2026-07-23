"""find-and-fix-bot — a standalone GitHub Copilot SDK harness.

Continuously scans a local repo/working tree for configurable *patterns*
(units of work). For each match it investigates the code in context and
proposes a fix as a unified diff, which the user can apply from the TUI.

The pattern this demonstrates (same as the pr_sentry exemplar it derives
from): your app owns data acquisition (a cheap regex/text scan, optionally
refined with structural analysis), change detection, caching and
presentation; the Copilot SDK is invoked purely for judgment — reading a
focused slice of code and deciding whether/how to fix it.
"""

__version__ = "0.1.0"
