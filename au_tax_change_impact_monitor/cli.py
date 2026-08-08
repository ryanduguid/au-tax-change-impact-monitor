from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import MonitorError
from .monitor import compare, validate_review, write_queue
from .util import path_within, repository_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a synthetic provenance-first tax change-review queue.")
    commands = parser.add_subparsers(dest="command", required=True)
    compare_parser = commands.add_parser("compare", help="compare a baseline index, observation, and exact source map")
    compare_parser.add_argument("--baseline", required=True, type=Path)
    compare_parser.add_argument("--observation", required=True, type=Path)
    compare_parser.add_argument("--map", required=True, type=Path)
    compare_parser.add_argument("--out", required=True, type=Path)
    review_parser = commands.add_parser("validate-review", help="validate a human technical-review decision")
    review_parser.add_argument("--queue", required=True, type=Path)
    review_parser.add_argument("--decision", required=True, type=Path)
    review_parser.add_argument("--out", type=Path, help="optional validation JSON below build/")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "compare":
            queue = compare(baseline_path=args.baseline, observation_path=args.observation, mapping_path=args.map)
            paths = write_queue(queue, args.out)
            print(f"au-tax-change-impact-monitor: {queue['run_status']}; {len(queue['items'])} item(s)")
            for name, path in paths.items():
                print(f"  {name}: {path}")
            return 0 if queue["run_status"] != "BLOCKED" else 2
        validation = validate_review(queue_path=args.queue, decision_path=args.decision)
        if args.out:
            output = path_within(args.out, repository_root() / "build", label="validation output", require_exists=False)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"au-tax-change-impact-monitor: {validation['status']}; {validation['decision_count']} decision(s)")
        return 0
    except MonitorError as exc:
        print(f"au-tax-change-impact-monitor: blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
