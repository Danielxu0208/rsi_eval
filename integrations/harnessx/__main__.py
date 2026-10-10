from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .prepare import prepare


def main():
    parser = argparse.ArgumentParser(description="Pinned public HarnessX evolution integration")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="clone and patch the optional source checkout; no model call")
    p.add_argument("--checkout", type=Path, default=Path(".runtime/method-sources/harnessx"))
    p.add_argument("--source", default="https://github.com/Darwin-Agent/HarnessX.git")
    for command in ("check", "run"):
        p = commands.add_parser(command)
        p.add_argument("config", type=Path)
        if command == "run":
            p.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.checkout, args.source)
    else:
        from .launcher import load_experiment, preflight, run, runtime_environment
        config = load_experiment(args.config)
        if args.command == "check":
            runtime_environment(config)
            result = preflight(config)
        else:
            result = asyncio.run(run(config, resume=args.resume))
    print(json.dumps(result, indent=2))
    if args.command == "check" and not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
