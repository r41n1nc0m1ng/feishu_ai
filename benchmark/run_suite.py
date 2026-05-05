from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


SUITE_TO_MODULE = {
    "full_demo": "benchmark.full_demo_dual_channel_test",
    "special_case": "benchmark.special_case.special_case_replay",
    "v2": "benchmark_v2.dual_channel_runner",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dispatch to one of the benchmark suites without remembering separate module paths."
    )
    parser.add_argument(
        "--suite",
        choices=sorted(SUITE_TO_MODULE),
        default="v2",
        help="Benchmark suite to run. Default uses the production benchmark v2 suite.",
    )
    parser.add_argument(
        "suite_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to the selected suite. Prefix with -- before suite args if needed.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    forwarded = list(args.suite_args or [])
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]

    cmd = [sys.executable, "-m", SUITE_TO_MODULE[args.suite], *forwarded]
    completed = subprocess.run(cmd, cwd=ROOT)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
