from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FIXTURE_PATH = ROOT / "full_demo_case_v2.json"
SOURCE_PATH = ROOT / "scenario_source_v2.json"


REQUIRED_TOP_LEVEL = [
    "schema_version",
    "case_id",
    "description",
    "chat_id",
    "replay_policy",
    "batches",
    "chat_profiles",
]

REQUIRED_BATCH_FIELDS = [
    "batch_id",
    "scenario",
    "fetch_time",
    "messages",
    "expected",
    "expected_brief",
]

REQUIRED_MESSAGE_FIELDS = [
    "message_id",
    "msg_type",
    "create_time",
    "sender",
    "content",
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_runtime_fixture(path: Path = FIXTURE_PATH) -> list[str]:
    errors: list[str] = []
    data = _load(path)

    for field in REQUIRED_TOP_LEVEL:
        if field not in data:
            errors.append(f"missing top-level field: {field}")

    batches = data.get("batches")
    if not isinstance(batches, list) or not batches:
        errors.append("batches must be a non-empty list")
        return errors

    chat_profiles = data.get("chat_profiles") or {}
    if not isinstance(chat_profiles, dict) or not chat_profiles:
        errors.append("chat_profiles must be a non-empty object")

    for i, batch in enumerate(batches):
        prefix = f"batch[{i}]"
        for field in REQUIRED_BATCH_FIELDS:
            if field not in batch:
                errors.append(f"{prefix} missing field: {field}")
        if "iceberg_context" in batch:
            errors.append(f"{prefix} should not contain iceberg_context in runtime fixture")

        msgs = batch.get("messages")
        if not isinstance(msgs, list) or not msgs:
            errors.append(f"{prefix}.messages must be a non-empty list")
            continue

        for j, msg in enumerate(msgs):
            mprefix = f"{prefix}.messages[{j}]"
            for field in REQUIRED_MESSAGE_FIELDS:
                if field not in msg:
                    errors.append(f"{mprefix} missing field: {field}")

        expected = batch.get("expected") or {}
        actions = expected.get("realtime_actions")
        if actions is not None and len(actions) != len(msgs):
            errors.append(
                f"{prefix} expected.realtime_actions length {len(actions)} != messages length {len(msgs)}"
            )
        if all(action in {"query", "topic_list"} for action in (actions or [])) and "topic_list" not in (batch.get("tags") or []) and expected.get("write_result_count") not in {0, None}:
            errors.append(f"{prefix} query-only batch should usually use write_result_count=0")

        expected_write = batch.get("expected_write_result") or {}
        for key in ("expected_memory_cards", "expected_relations", "should_ignore_message_ids"):
            if key in expected_write and not isinstance(expected_write.get(key), list):
                errors.append(f"{prefix}.expected_write_result.{key} must be a list")

        batch_chat_id = batch.get("chat_id") or data.get("chat_id")
        if batch_chat_id and batch_chat_id not in chat_profiles:
            errors.append(f"{prefix} chat_id {batch_chat_id} missing in chat_profiles")

        if "query" in (batch.get("tags") or []) and "query" not in (expected.get("realtime_actions") or []) and "topic_list" not in (expected.get("realtime_actions") or []):
            errors.append(f"{prefix} tagged query but expected.realtime_actions does not include query/topic_list")

        if expected_write.get("expected_memory_cards"):
            for k, item in enumerate(expected_write["expected_memory_cards"]):
                if not isinstance(item, dict):
                    errors.append(f"{prefix}.expected_write_result.expected_memory_cards[{k}] must be an object")
                    continue
                if not item.get("expected_keywords"):
                    errors.append(
                        f"{prefix}.expected_write_result.expected_memory_cards[{k}] missing expected_keywords"
                    )

        if expected_write.get("expected_relations"):
            for k, item in enumerate(expected_write["expected_relations"]):
                if not isinstance(item, dict):
                    errors.append(f"{prefix}.expected_write_result.expected_relations[{k}] must be an object")
                    continue
                if not item.get("relation_type"):
                    errors.append(
                        f"{prefix}.expected_write_result.expected_relations[{k}] missing relation_type"
                    )

    return errors


def validate_source_fixture(path: Path = SOURCE_PATH) -> list[str]:
    errors: list[str] = []
    data = _load(path)
    batches = data.get("batches")
    if not isinstance(batches, list) or not batches:
        return ["source batches must be a non-empty list"]

    for i, batch in enumerate(batches):
        prefix = f"source.batch[{i}]"
        if "iceberg_context" not in batch:
            errors.append(f"{prefix} missing iceberg_context")
            continue
        ctx = batch["iceberg_context"]
        for field in [
            "project_name",
            "tenant_type",
            "delivery_window",
            "primary_group_goal",
            "scene",
            "work_item",
            "deliverables",
            "participants",
            "hidden_tasks",
            "off_group_events",
            "message_projection_rule",
            "hidden_state",
        ]:
            if field not in ctx:
                errors.append(f"{prefix}.iceberg_context missing field: {field}")

    top_expected = data.get("expected") or {}
    for key in ("final_memory_checks", "relation_checks"):
        if key in top_expected and not isinstance(top_expected.get(key), list):
            errors.append(f"source.expected.{key} must be a list")
    return errors


def main() -> None:
    runtime_errors = validate_runtime_fixture()
    source_errors = validate_source_fixture()
    all_errors = runtime_errors + source_errors
    if all_errors:
        print(json.dumps({"ok": False, "errors": all_errors}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    print(json.dumps({"ok": True, "runtime": str(FIXTURE_PATH), "source": str(SOURCE_PATH)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
