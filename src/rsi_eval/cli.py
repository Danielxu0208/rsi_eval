from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from .benchmarks import list_benchmarks
from .config import load_config
from .evaluation import check, evaluate, preview


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rsi-eval", description="Run agent benchmarks without owning the improvement loop.")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List bundled, pinned benchmark configurations")
    listing.add_argument("--json", action="store_true")
    status = commands.add_parser("status", help="Show recorded compatibility and scoped validation evidence")
    status.add_argument("--json", action="store_true")
    status.add_argument("--benchmark", help="Benchmark family or exact name@version")
    status.add_argument("--method", help="Managed method name")
    from .methods import add_commands
    add_commands(commands)
    from .portability import add_portability_commands
    add_portability_commands(commands)
    from .runtime_snapshot import add_commands as add_runtime_commands
    add_runtime_commands(commands)
    resuming = commands.add_parser("resume", help="Resume only unfinished trials from a recorded evaluation")
    resuming.add_argument("--output", required=True, type=Path)
    for name in ("check", "preview", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", required=True, type=Path)
        sub.add_argument("--output", type=Path)
        sub.add_argument("--oracle", action="store_true", help="Run the task's declared oracle/reference solution to validate its verifier")
        if name == "run":
            sub.add_argument("--resume", action="store_true", help="Require the same request and continue unfinished trials")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            from .support_status import format_status, support_status
            record = support_status(benchmark=args.benchmark, method=args.method)
            print(json.dumps(record, indent=2) if args.json else format_status(
                record, detailed=bool(args.benchmark or args.method)))
            return 0
        if args.command == "experiment":
            from .portability import dispatch_portability
            return dispatch_portability(args)
        if args.command == "runtime":
            from .runtime_snapshot import dispatch as dispatch_runtime
            return dispatch_runtime(args)
        if args.command in {"configure", "method"}:
            from .methods import dispatch
            return dispatch(args)
        if args.command == "resume":
            from .resume import resume_evaluation
            result = resume_evaluation(args.output)
            print(json.dumps({"status": result.status, "report": result.report_path, "error": result.error}, indent=2))
            return 0 if result.status == "completed" else 1
        if args.command == "list":
            rows = [{k: v for k, v in s.items() if k != "tasks"} | {"task_count": len(s["tasks"])} for s in list_benchmarks()]
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for row in rows:
                    lifecycle = {"legacy-subset": " [legacy subset]",
                                 "grading-blocked": " [grading blocked]"}.get(row.get("lifecycle"), "")
                    print(f"{row['name']}@{row['version']}\t{row['task_count']} tasks{lifecycle}\t{row['dataset_ref']}")
            return 0
        target, benchmark, options, output = load_config(args.config)
        if args.oracle:
            target = replace(target, entrypoint="oracle", model=None, config={})
        if args.output is not None:
            output = args.output.resolve()
        if args.command == "check":
            result = check(target=target, benchmark=benchmark, options=options)
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        if args.command == "preview":
            result = preview(target=target, benchmark=benchmark, options=options, output_dir=output)
            print(json.dumps(result, indent=2, default=str))
            return 0
        result = evaluate(target=target, benchmark=benchmark, options=options, output_dir=output, resume=args.resume)
        print(json.dumps({"status": result.status, "report": result.report_path,
                          "native_artifacts": result.artifact_dir, "error": result.error}, indent=2))
        return 0 if result.status == "completed" else 1
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as error:
        print(f"rsi-eval: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
