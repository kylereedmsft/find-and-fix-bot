"""Entry point: `python -m findfix` (or `find-and-fix` after install)."""

from __future__ import annotations

import argparse
import asyncio
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from .app import FindFixApp
from .config import load_work_configs
from .tui import FindFixTUI


def main() -> None:
    p = argparse.ArgumentParser(
        prog="find-and-fix",
        description="Standalone Copilot-SDK harness: continuously scans a local repo for "
        "configurable patterns (units of work), investigates each match, and proposes a "
        "fix you can apply. One tab per unit of work.",
    )
    p.add_argument("--config", default=None, help="path to work config JSON (default findfix.config.json)")
    p.add_argument("--interval", type=int, default=60, help="scan interval in seconds (default 60)")
    p.add_argument("--model", default=None, help="override Copilot model")
    p.add_argument("--work", action="append", help="restrict to work unit label(s); repeatable")
    p.add_argument("--once", action="store_true", help="scan + investigate once, print summary, exit")
    args = p.parse_args()

    works = load_work_configs(args.config)
    if args.work:
        wanted = set(args.work)
        works = [w for w in works if w.label in wanted]
    if not works:
        print("No matching work units configured.", file=sys.stderr)
        sys.exit(2)

    apps = [FindFixApp(w, interval=args.interval, model=args.model) for w in works]

    if args.once:

        async def once() -> None:
            for a in apps:
                async with a._investigator:  # noqa: SLF001
                    await a._scan_once()      # noqa: SLF001
                print(f"\n=== {a.work.label} ===")
                for item in a.state.ordered():
                    m, r = item.match, item.resolution
                    print(f"[{r.verdict.value:>8}] {m.path}:{m.line}  {m.matched_text or m.reason}")
                    if r.explanation:
                        print(f"           {r.explanation}")
                    if r.has_fix:
                        print(f"           (proposed diff, {r.diff.count(chr(10)) + 1} lines)")
                    if r.error:
                        print(f"           ! {r.error}")
                if a.state.error:
                    print(f"ERROR: {a.state.error}", file=sys.stderr)

        asyncio.run(once())
        return

    FindFixTUI(apps).run()


if __name__ == "__main__":
    main()
