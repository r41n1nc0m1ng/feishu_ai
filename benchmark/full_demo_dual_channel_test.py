from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from benchmark.replay_adapter import DualChannelReplayAdapter, ReplayResult
except ModuleNotFoundError:
    from replay_adapter import DualChannelReplayAdapter, ReplayResult


DEFAULT_FIXTURE_PATH = Path(__file__).with_name("full_demo_case.json")


def load_benchmark_case(fixture_path: str | Path) -> dict[str, Any]:
    path = Path(fixture_path)
    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text:
        raise ValueError(f"fixture is empty: {path}")

    case = json.loads(raw_text)
    if not isinstance(case, dict):
        raise ValueError(f"fixture root must be an object: {path}")
    if not isinstance(case.get("batches"), list):
        raise ValueError(f"fixture must contain a batches list: {path}")
    return case


class DualChannelReplayRunner:
    """
    Orchestrates dual-channel replay order only.

    The runner intentionally does not know FeishuMessage, FetchBatch,
    dispatch_message, segment, or any later memory pipeline detail. It passes
    raw fixture data to the adapter, and the adapter owns conversion plus entry
    calls.
    """

    def __init__(self, adapter: DualChannelReplayAdapter | None = None):
        self.adapter = adapter or DualChannelReplayAdapter()
        self.batch_results: list[dict[str, Any]] = []

    async def run_case(self, fixture_path: str | Path = DEFAULT_FIXTURE_PATH) -> dict[str, Any]:
        case = load_benchmark_case(fixture_path)
        batches = case.get("batches") or []
        print(f"loaded {len(batches)} batches from {fixture_path}")

        for batch in batches:
            await self.run_batch(case, batch)

        failures = [result for result in self.batch_results if not result["success"]]
        summary = {
            "case_id": case.get("case_id", ""),
            "total_batches": len(self.batch_results),
            "total_realtime_messages": sum(item["realtime_sent"] for item in self.batch_results),
            "total_realtime_skipped": sum(item["realtime_skipped"] for item in self.batch_results),
            "total_write_messages": sum(item["write_input_count"] for item in self.batch_results),
            "overall_success": not failures,
            "batch_results": self.batch_results,
        }

        print("\n=== dual channel replay summary ===")
        print(f"batches: {summary['total_batches']}")
        print(f"realtime sent: {summary['total_realtime_messages']}")
        print(f"realtime skipped: {summary['total_realtime_skipped']}")
        print(f"write messages: {summary['total_write_messages']}")
        print(f"result: {'PASS' if summary['overall_success'] else 'FAIL'}")
        return summary

    async def run_batch(self, case: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
        batch_id = str(batch.get("batch_id", ""))
        raw_messages = batch.get("messages") or []

        print(f"\n=== batch {batch_id} ===")
        print(f"raw messages={len(raw_messages)}")

        realtime_results: list[ReplayResult] = []
        for raw_msg in raw_messages:
            result = await self.adapter.send_realtime_message(raw_msg, case=case, batch=batch)
            realtime_results.append(result)
            if result.skipped:
                print(f"  realtime skipped: {result.message_id}")
            elif result.ok:
                print(f"  realtime sent: {result.message_id} -> {result.action}")
            else:
                print(f"  realtime error: {result.message_id} -> {result.error}")

        write_result = await self.adapter.send_write_batch(batch, case=case)
        if write_result.ok:
            print(
                "  write batch sent: "
                f"input_messages={write_result.input_count}; "
                f"result_count={write_result.result_count}"
            )
        else:
            print(f"  write batch error: {write_result.error}")

        failures = self._collect_failures(batch, realtime_results, write_result)
        result = {
            "batch_id": batch_id,
            "raw_message_count": len(raw_messages),
            "realtime_sent": sum(1 for item in realtime_results if item.ok and not item.skipped),
            "realtime_skipped": sum(1 for item in realtime_results if item.skipped),
            "realtime_actions": [item.action for item in realtime_results if item.ok and not item.skipped],
            "write_input_count": write_result.input_count,
            "write_result_count": write_result.result_count,
            "write_ignored_message_ids": write_result.ignored_message_ids,
            "success": not failures,
            "failures": failures,
        }
        self.batch_results.append(result)
        return result

    def _collect_failures(
        self,
        batch: dict[str, Any],
        realtime_results: list[ReplayResult],
        write_result: ReplayResult,
    ) -> list[str]:
        failures = [
            f"realtime error {item.message_id}: {item.error}"
            for item in realtime_results
            if not item.ok
        ]
        if not write_result.ok:
            failures.append(f"write error: {write_result.error}")

        expected = batch.get("expected") or {}
        expected_actions = expected.get("realtime_actions")
        if expected_actions is not None:
            actual_actions = [item.action for item in realtime_results if item.ok and not item.skipped]
            if actual_actions != expected_actions:
                failures.append(f"realtime_actions expected {expected_actions}, got {actual_actions}")

        expected_write_count = expected.get("write_result_count")
        if expected_write_count is not None and write_result.result_count != expected_write_count:
            failures.append(
                f"write_result_count expected {expected_write_count}, got {write_result.result_count}"
            )

        return failures


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full demo dual-channel replay.")
    parser.add_argument("fixture", nargs="?", default=str(DEFAULT_FIXTURE_PATH))
    args = parser.parse_args()

    summary = await DualChannelReplayRunner().run_case(args.fixture)
    if not summary["overall_success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
