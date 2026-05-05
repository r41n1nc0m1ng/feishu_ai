from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SOURCE_PATH = ROOT / "scenario_source_v2.json"
RUNTIME_PATH = ROOT / "full_demo_case_v2.json"


HIGH_RISK_MIXED_TAGS = {
    ("query", "scope"),
    ("query", "supersede"),
    ("schedule", "multi_topic"),
    ("task", "multi_topic"),
}

NON_ENGINEERING_TAGS = {
    "product_launch",
    "ops",
    "customer_success",
    "meeting_org",
    "roadshow",
    "non_engineering",
    "policy",
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def lint_case(data: dict[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    infos: list[str] = []
    batches = data.get("batches") or []
    chat_profiles = data.get("chat_profiles") or {}

    tag_counter: Counter[str] = Counter()
    chat_counter: Counter[str] = Counter()
    chat_tags: dict[str, set[str]] = defaultdict(set)
    query_only_batches = 0
    non_engineering_batches = 0

    for batch in batches:
        batch_id = str(batch.get("batch_id", ""))
        tags = set(batch.get("tags") or [])
        messages = batch.get("messages") or []
        chat_id = str(batch.get("chat_id") or data.get("chat_id") or "")
        chat_counter[chat_id] += 1
        chat_tags[chat_id].update(tags)
        tag_counter.update(tags)

        expected = batch.get("expected") or {}
        actions = list(expected.get("realtime_actions") or [])
        write_count = expected.get("write_result_count")

        if actions and all(action in {"query", "topic_list"} for action in actions):
            query_only_batches += 1

        if tags & NON_ENGINEERING_TAGS:
            non_engineering_batches += 1

        if ("schedule" in tags or "task" in tags) and write_count not in {0, 1}:
            warnings.append(f"{batch_id}: action-heavy batch has unusual write_result_count={write_count}")

        if len(messages) == 1 and "multi_topic" in tags:
            warnings.append(f"{batch_id}: multi_topic batch has only one message")

        for combo in HIGH_RISK_MIXED_TAGS:
            if set(combo).issubset(tags):
                warnings.append(f"{batch_id}: high-risk mixed tags {combo}, verify this matches real group logic")

    if len(chat_profiles) < 4:
        warnings.append("chat_profiles fewer than 4 groups; production benchmark should separate more collaboration surfaces")

    if non_engineering_batches < 4:
        warnings.append("non-engineering coverage is still thin; add more ops / policy / event / business scenarios")

    if query_only_batches < 4:
        warnings.append("query-only followup coverage is thin")

    if tag_counter.get("topic_list", 0) and query_only_batches == 0:
        warnings.append("topic_list appears without any query-only coverage")

    for chat_id, tags in chat_tags.items():
        profile = chat_profiles.get(chat_id) or {}
        theme = str(profile.get("theme") or "")
        if not theme:
            warnings.append(f"{chat_id}: missing chat profile theme")
        if len(tags) > 10 and "压测" not in theme and "压力" not in theme:
            infos.append(f"{chat_id}: tag span is wide ({len(tags)} tags); verify topic boundary realism")

    infos.append(f"total_batches={len(batches)}")
    infos.append(f"distinct_chats={len(chat_counter)}")
    infos.append(f"non_engineering_batches={non_engineering_batches}")
    infos.append(f"query_only_batches={query_only_batches}")
    infos.append(f"top_tags={dict(tag_counter.most_common(12))}")

    return {
        "ok": not warnings,
        "warnings": warnings,
        "infos": infos,
    }


def main() -> None:
    source = _load(SOURCE_PATH)
    runtime = _load(RUNTIME_PATH)
    source_result = lint_case(source)
    runtime_result = lint_case(runtime)
    payload = {
        "source": source_result,
        "runtime": runtime_result,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if source_result["ok"] and runtime_result["ok"] else 1)


if __name__ == "__main__":
    main()
