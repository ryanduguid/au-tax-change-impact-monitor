from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import MonitorError
from .monitor import compare, validate_review, write_queue


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
    review_parser.add_argument("--out", type=Path, help="optional path for the validation JSON")
    return parser


def _blocked(message: str) -> int:
    print(f"au-tax-change-impact-monitor: blocked: {message}", file=sys.stderr)
    return 2


def _say(line: str) -> None:
    """Write one stdout line without letting an unprintable path fail the run.

    A redirected (non-console) stdout on Windows defaults to the ANSI code page
    with errors='strict', so printing an --out path holding a macron vowel,
    Cyrillic or CJK character raised UnicodeEncodeError and exited 1 after both
    queue files had already been written correctly.

    Only the offending line is escaped, and stderr says so, because a caller
    reading the queue location off stdout must be able to tell an exact path
    from an escaped rendering of one. sys.stdout is deliberately not
    reconfigured: that would silently change every later writer in the process,
    including an embedding application's, for the rest of its life.
    """
    try:
        print(line)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(encoding, "backslashreplace").decode(encoding))
        print(
            f"au-tax-change-impact-monitor: note: the line above is backslash-escaped because "
            f"stdout encoding {encoding} cannot represent it; read the path from the filesystem, not from stdout.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "compare":
            queue = compare(baseline_path=args.baseline, observation_path=args.observation, mapping_path=args.map)
            # Scoped to the output write alone. Everything upstream of it turns
            # its own OSErrors into MonitorError, and an OSError raised anywhere
            # else - a BrokenPipeError from the prints below, say - is not a
            # path problem and must not be labelled as one.
            try:
                paths = write_queue(queue, args.out)
            except OSError as exc:
                return _blocked(f"the output directory could not be written: {exc}")
            _say(f"au-tax-change-impact-monitor: {queue['run_status']}; {len(queue['items'])} item(s)")
            for name, path in paths.items():
                _say(f"  {name}: {path}")
            return 0 if queue["run_status"] != "BLOCKED" else 2
        validation = validate_review(queue_path=args.queue, decision_path=args.decision)
        if args.out:
            try:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            except OSError as exc:
                return _blocked(f"the validation output file could not be written: {exc}")
        _say(f"au-tax-change-impact-monitor: {validation['status']}; {validation['decision_count']} decision(s)")
        return 0
    except MonitorError as exc:
        return _blocked(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
