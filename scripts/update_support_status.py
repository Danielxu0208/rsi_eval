#!/usr/bin/env python3
"""Regenerate the guide to packaged compatibility and runtime requirements."""
import argparse
from pathlib import Path

from rsi_eval.support_status import render_markdown, support_status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail when the committed page differs")
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / "docs/support-status.md"
    rendered = render_markdown(support_status())
    if args.check:
        if not output.exists() or output.read_text() != rendered:
            parser.exit(1, "Support status documentation is stale; run scripts/update_support_status.py\n")
    else:
        output.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
