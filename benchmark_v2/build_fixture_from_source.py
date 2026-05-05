from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_PATH = ROOT / "scenario_source_v2.json"
FIXTURE_PATH = ROOT / "full_demo_case_v2.json"


TOP_LEVEL_DROP_FIELDS = {
    "iceberg_policy",
}

BATCH_DROP_FIELDS = {
    "iceberg_context",
}


def build_fixture(source_path: Path = SOURCE_PATH, fixture_path: Path = FIXTURE_PATH) -> None:
    data = json.loads(source_path.read_text(encoding="utf-8"))
    runtime = dict(data)

    for field in TOP_LEVEL_DROP_FIELDS:
        runtime.pop(field, None)

    cleaned_batches = []
    for batch in runtime.get("batches", []):
        cleaned = dict(batch)
        for field in BATCH_DROP_FIELDS:
            cleaned.pop(field, None)
        if "expected" in cleaned and "expected_realtime_results" not in cleaned:
            actions = list((cleaned.get("expected") or {}).get("realtime_actions") or [])
            messages = list(cleaned.get("messages") or [])
            if actions and len(actions) == len(messages):
                cleaned["expected_realtime_results"] = [
                    {
                        "message_id": msg.get("message_id", ""),
                        "expected_realtime_action": action,
                    }
                    for msg, action in zip(messages, actions)
                ]
        cleaned_batches.append(cleaned)
    runtime["batches"] = cleaned_batches

    fixture_path.write_text(
        json.dumps(runtime, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    build_fixture()
    print(f"built fixture: {FIXTURE_PATH}")
