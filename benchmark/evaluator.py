"""
评估脚本：对比 result.json 与 full_demo_case.json expected 部分，输出 evaluation.json。

评估规则：
  memory_card / topic_summary：bot 回复中包含 expected_keywords 中至少一个关键词（pass），
                                且不包含 forbidden_keywords 中任何关键词（pass）。
  evidence_block：actual_source_message_ids 与 expected_source_message_ids 集合完全吻合。
  relation_check：found == True。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_RESULT  = Path(__file__).with_name("result.json")
DEFAULT_FIXTURE = Path(__file__).with_name("full_demo_case.json")
DEFAULT_OUTPUT  = Path(__file__).with_name("evaluation.json")


# ── 单项评估函数 ──────────────────────────────────────────────────────────────

def _eval_keyword_check(reply: str, expected_kws: list[str], forbidden_kws: list[str]) -> dict:
    matched   = [kw for kw in expected_kws if kw in reply]
    forbidden = [kw for kw in forbidden_kws if kw in reply]
    return {
        "expected_keywords":  expected_kws,
        "matched_keywords":   matched,
        "keyword_pass":       bool(matched) if expected_kws else True,
        "forbidden_keywords": forbidden_kws,
        "forbidden_found":    forbidden,
        "forbidden_pass":     not bool(forbidden),
    }


def _type_alignment_check(entry: dict) -> dict:
    expected = entry.get("granularity", "")
    actual   = entry.get("bot_reply_memory_type", "")
    passed   = (actual == expected) if actual else None  # None = 无数据，不计入
    return {
        "expected_granularity":  expected,
        "actual_memory_type":    actual,
        "type_aligned":          passed,
    }


def _eval_memory_or_topic(check_entry: dict, topic_summaries: list[dict] | None = None) -> dict:
    granularity = check_entry.get("granularity", "")
    exp_kws     = (check_entry.get("expected_keywords") or
                   check_entry.get("expected_decision_object_keywords") or [])
    forb_kws    = check_entry.get("forbidden_keywords", [])

    if granularity == "topic_summary" and topic_summaries:
        # 将所有 TopicSummary 拼接为一个文本做关键词匹配，比 bot 回复更可靠
        combined = " ".join(
            f"{t.get('topic','')} {t.get('summary','')}" for t in topic_summaries
        )
        kw_result = _eval_keyword_check(combined, exp_kws, forb_kws)
        # bot 回复作为补充参考（不影响 pass/fail）
        kw_result["topic_summary_source"] = [
            {"topic": t.get("topic",""), "summary": t.get("summary","")[:80]}
            for t in topic_summaries
        ]
    else:
        reply     = check_entry.get("bot_reply", "")
        kw_result = _eval_keyword_check(reply, exp_kws, forb_kws)

    type_check = _type_alignment_check(check_entry)
    passed = (kw_result["keyword_pass"] and kw_result["forbidden_pass"]
              and type_check["type_aligned"] is not False)
    return {
        "check_type":  "final_memory_check",
        "query":       check_entry.get("query", ""),
        "granularity": granularity,
        "pass":        passed,
        **kw_result,
        **type_check,
    }


def _eval_evidence(check_entry: dict) -> dict:
    actual   = set(check_entry.get("actual_source_message_ids", []))
    expected = set(check_entry.get("expected_source_message_ids", []))
    exact    = actual == expected
    missing  = sorted(expected - actual)
    extra    = sorted(actual - expected)
    # 关键词也检查（有则兼顾）
    reply   = check_entry.get("bot_reply", "")
    exp_kws = check_entry.get("expected_keywords", [])
    kw_pass = any(kw in reply for kw in exp_kws) if exp_kws else True
    type_check = _type_alignment_check(check_entry)
    return {
        "check_type":             "evidence_check",
        "query":                  check_entry.get("query", ""),
        "granularity":            "evidence_block",
        "pass":                   exact and kw_pass and type_check["type_aligned"] is not False,
        "id_exact_match":         exact,
        "actual_ids":             sorted(actual),
        "expected_ids":           sorted(expected),
        "missing_ids":            missing,
        "extra_ids":              extra,
        "keyword_pass":           kw_pass,
        **type_check,
    }


def _eval_relation(check_entry: dict) -> dict:
    return {
        "check_type":    "relation_check",
        "relation_type": check_entry.get("relation_type", ""),
        "pass":          bool(check_entry.get("found", False)),
        "detail":        check_entry,
    }


# ── 主评估函数 ────────────────────────────────────────────────────────────────

def run_evaluation(
    result_path:  str | Path = DEFAULT_RESULT,
    fixture_path: str | Path = DEFAULT_FIXTURE,
    output_path:  str | Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:

    result  = json.loads(Path(result_path).read_text(encoding="utf-8"))
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))

    checks: list[dict] = []

    expected_checks  = result.get("expected_checks", {})
    topic_summaries  = result.get("topic_summaries", [])

    # final_memory_checks（memory_card / topic_summary）
    for entry in expected_checks.get("final_memory_checks", []):
        checks.append(_eval_memory_or_topic(entry, topic_summaries))

    # relation_checks
    for entry in expected_checks.get("relation_checks", []):
        checks.append(_eval_relation(entry))

    # evidence_checks
    for entry in expected_checks.get("evidence_checks", []):
        checks.append(_eval_evidence(entry))

    # 汇总
    total  = len(checks)
    passed = sum(1 for c in checks if c.get("pass"))
    failed = total - passed

    evaluation: dict[str, Any] = {
        "case_id": result.get("case_id", ""),
        "summary": {
            "total":  total,
            "passed": passed,
            "failed": failed,
            "pass_rate": f"{passed / total:.0%}" if total else "N/A",
            "overall_pass": failed == 0,
        },
        "checks": checks,
    }

    Path(output_path).write_text(
        json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n=== 评估结果 ===")
    print(f"总计 {total} 项 | 通过 {passed} | 失败 {failed} | 通过率 {evaluation['summary']['pass_rate']}")
    for c in checks:
        status = "PASS" if c.get("pass") else "FAIL"
        label  = c.get("query") or c.get("relation_type", "")
        print(f"  [{status}] {c['check_type']} | {label[:50]}")
    print(f"评估报告已写入: {output_path}")

    return evaluation


if __name__ == "__main__":
    run_evaluation()
