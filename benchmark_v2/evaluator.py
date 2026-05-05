from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from memory import store
from memory.schemas import CardStatus, MemoryCard, MemoryRelation, TopicSummary


def _normalize_text(value: str) -> str:
    return (value or "").strip().lower()


def _card_text(card: MemoryCard) -> str:
    return " ".join(
        [
            card.decision_object,
            card.title,
            card.decision,
            card.reason,
        ]
    ).lower()


def _topic_text(topic: TopicSummary) -> str:
    return f"{topic.topic} {topic.summary}".lower()


def _keywords_match(text: str, expected_keywords: list[str] | None) -> bool:
    if not expected_keywords:
        return True
    normalized = _normalize_text(text)
    return all(_normalize_text(keyword) in normalized for keyword in expected_keywords)


def _keywords_absent(text: str, forbidden_keywords: list[str] | None) -> bool:
    if not forbidden_keywords:
        return True
    normalized = _normalize_text(text)
    return all(_normalize_text(keyword) not in normalized for keyword in forbidden_keywords)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class EvaluatorSummary:
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class BenchmarkEvaluator:
    def __init__(self) -> None:
        self.deep_eval_enabled = os.getenv("BENCHMARK_V2_DEEP_EVAL", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def evaluate_batch(
        self,
        *,
        case: dict[str, Any],
        batch: dict[str, Any],
        write_result: Any,
        realtime_actions: list[str],
    ) -> EvaluatorSummary:
        chat_id = str(batch.get("chat_id") or case.get("chat_id") or "")
        checks: list[CheckResult] = []

        checks.extend(self._check_realtime(case, batch, realtime_actions))
        checks.extend(self._check_write_result(batch, write_result))
        checks.extend(self._check_memory_cards(chat_id, batch))
        checks.extend(self._check_relations(chat_id, batch))
        checks.extend(self._check_topics(chat_id, batch))

        passed = all(check.passed for check in checks)
        metrics = {
            "total_checks": len(checks),
            "passed_checks": sum(1 for check in checks if check.passed),
            "failed_checks": sum(1 for check in checks if not check.passed),
        }
        return EvaluatorSummary(passed=passed, checks=checks, metrics=metrics)

    def evaluate_case(self, case: dict[str, Any]) -> EvaluatorSummary:
        checks: list[CheckResult] = []
        if not self.deep_eval_enabled:
            checks.append(CheckResult(name="case_deep_eval", passed=True, detail="skipped"))
            return EvaluatorSummary(
                passed=True,
                checks=checks,
                metrics={"total_checks": 1, "passed_checks": 1, "failed_checks": 0},
            )

        final_expected = case.get("expected") or {}
        final_memory_checks = final_expected.get("final_memory_checks") or []
        relation_checks = final_expected.get("relation_checks") or []
        evidence_checks = final_expected.get("evidence_checks") or []

        for i, spec in enumerate(final_memory_checks):
            chat_id = str(spec.get("chat_id") or case.get("chat_id") or "")
            cards = store.get_cards_for_chat(chat_id)
            topics = store.load_topics_by_chat(chat_id)
            target = spec.get("expected_granularity", "memory_card")
            expected_keywords = spec.get("expected_keywords") or []
            forbidden_keywords = spec.get("forbidden_keywords") or []

            if target == "topic_summary":
                matched = any(
                    _keywords_match(_topic_text(topic), expected_keywords)
                    and _keywords_absent(_topic_text(topic), forbidden_keywords)
                    for topic in topics
                )
            else:
                matched = any(
                    _keywords_match(_card_text(card), expected_keywords)
                    and _keywords_absent(_card_text(card), forbidden_keywords)
                    for card in cards
                )
            checks.append(
                CheckResult(
                    name=f"final_memory_checks[{i}]",
                    passed=matched,
                    detail=str(spec.get("query") or ""),
                )
            )

        if relation_checks:
            checks.extend(self._check_relation_specs(case, relation_checks, prefix="case_relation_checks"))
        if evidence_checks:
            checks.extend(self._check_evidence_specs(case, evidence_checks, prefix="case_evidence_checks"))

        passed = all(check.passed for check in checks)
        return EvaluatorSummary(
            passed=passed,
            checks=checks,
            metrics={
                "total_checks": len(checks),
                "passed_checks": sum(1 for check in checks if check.passed),
                "failed_checks": sum(1 for check in checks if not check.passed),
            },
        )

    def _check_realtime(
        self,
        case: dict[str, Any],
        batch: dict[str, Any],
        realtime_actions: list[str],
    ) -> list[CheckResult]:
        expected = batch.get("expected") or {}
        checks: list[CheckResult] = []
        expected_actions = expected.get("realtime_actions")
        if expected_actions is not None:
            checks.append(
                CheckResult(
                    name="realtime_actions",
                    passed=realtime_actions == expected_actions,
                    detail=f"expected={expected_actions} actual={realtime_actions}",
                )
            )
        return checks

    def _check_write_result(self, batch: dict[str, Any], write_result: Any) -> list[CheckResult]:
        expected = batch.get("expected") or {}
        checks: list[CheckResult] = []
        expected_write = expected.get("write_result_count")
        if expected_write is not None:
            actual = getattr(write_result, "result_count", 0)
            checks.append(
                CheckResult(
                    name="write_result_count",
                    passed=actual == expected_write,
                    detail=f"expected={expected_write} actual={actual}",
                )
            )

        expected_write_result = batch.get("expected_write_result") or {}
        should_ignore = sorted(expected_write_result.get("should_ignore_message_ids") or [])
        if should_ignore:
            actual_ignored = sorted(getattr(write_result, "ignored_message_ids", []) or [])
            checks.append(
                CheckResult(
                    name="should_ignore_message_ids",
                    passed=all(message_id in actual_ignored for message_id in should_ignore),
                    detail=f"expected_subset={should_ignore} actual={actual_ignored}",
                )
            )
        return checks

    def _check_memory_cards(self, chat_id: str, batch: dict[str, Any]) -> list[CheckResult]:
        expected_write = batch.get("expected_write_result") or {}
        specs = expected_write.get("expected_memory_cards") or []
        optional_specs = expected_write.get("optional_progress_cards") or []
        if not self.deep_eval_enabled:
            if not specs and not optional_specs:
                return []
            return [CheckResult(name="memory_card_eval", passed=True, detail="skipped")]
        cards = store.get_cards_for_chat(chat_id)
        checks: list[CheckResult] = []

        for i, spec in enumerate(specs):
            expected_keywords = spec.get("expected_keywords") or []
            forbidden_keywords = spec.get("forbidden_keywords") or []
            expected_status = spec.get("expected_status")
            matched_cards = []
            for card in cards:
                if expected_status and card.status.value != expected_status:
                    continue
                text = _card_text(card)
                if _keywords_match(text, expected_keywords) and _keywords_absent(text, forbidden_keywords):
                    matched_cards.append(card)
            checks.append(
                CheckResult(
                    name=f"expected_memory_cards[{i}]",
                    passed=bool(matched_cards),
                    detail=", ".join(card.memory_id for card in matched_cards[:3]) or str(expected_keywords),
                )
            )

        for i, spec in enumerate(optional_specs):
            expected_keywords = spec.get("expected_keywords") or []
            matched = any(_keywords_match(_card_text(card), expected_keywords) for card in cards)
            checks.append(
                CheckResult(
                    name=f"optional_progress_cards[{i}]",
                    passed=True,
                    detail="matched" if matched else "optional-not-produced",
                )
            )
        return checks

    def _check_relations(self, chat_id: str, batch: dict[str, Any]) -> list[CheckResult]:
        expected_write = batch.get("expected_write_result") or {}
        specs = expected_write.get("expected_relations") or []
        if not self.deep_eval_enabled:
            if not specs:
                return []
            return [CheckResult(name="relation_eval", passed=True, detail="skipped")]
        return self._check_relation_specs({"chat_id": chat_id}, specs, prefix="expected_relations")

    def _check_relation_specs(
        self,
        case: dict[str, Any],
        specs: list[dict[str, Any]],
        *,
        prefix: str,
    ) -> list[CheckResult]:
        chat_id = str(case.get("chat_id") or "")
        cards = {card.memory_id: card for card in store.get_cards_for_chat(chat_id)}
        relations = store.load_relations_by_chat(chat_id)
        checks: list[CheckResult] = []

        for i, spec in enumerate(specs):
            relation_type = spec.get("relation_type")
            old_keywords = spec.get("old_expected_keywords") or []
            new_keywords = spec.get("new_expected_keywords") or []
            matched = False
            for relation in relations:
                if relation.relation_type.value != relation_type:
                    continue
                source_card = cards.get(relation.source_id) or store.load_memory_card(relation.source_id)
                target_card = cards.get(relation.target_id) or store.load_memory_card(relation.target_id)
                if not source_card or not target_card:
                    continue
                if new_keywords and not _keywords_match(_card_text(source_card), new_keywords):
                    continue
                if old_keywords and not _keywords_match(_card_text(target_card), old_keywords):
                    continue
                matched = True
                break
            checks.append(
                CheckResult(
                    name=f"{prefix}[{i}]",
                    passed=matched,
                    detail=f"{relation_type} old={old_keywords} new={new_keywords}",
                )
            )
        return checks

    def _check_topics(self, chat_id: str, batch: dict[str, Any]) -> list[CheckResult]:
        expected_write = batch.get("expected_write_result") or {}
        specs = expected_write.get("expected_topic_summaries") or []
        if not self.deep_eval_enabled:
            if not specs:
                return []
            return [CheckResult(name="topic_eval", passed=True, detail="skipped")]
        topics = store.load_topics_by_chat(chat_id)
        checks: list[CheckResult] = []
        for i, spec in enumerate(specs):
            expected_keywords = spec.get("expected_keywords") or []
            matched = any(_keywords_match(_topic_text(topic), expected_keywords) for topic in topics)
            checks.append(
                CheckResult(
                    name=f"expected_topic_summaries[{i}]",
                    passed=matched,
                    detail=str(expected_keywords),
                )
            )
        return checks

    def _check_evidence_specs(
        self,
        case: dict[str, Any],
        specs: list[dict[str, Any]],
        *,
        prefix: str,
    ) -> list[CheckResult]:
        chat_id = str(case.get("chat_id") or "")
        cards = store.get_cards_for_chat(chat_id)
        checks: list[CheckResult] = []
        for i, spec in enumerate(specs):
            source_message_ids = set(spec.get("source_message_ids") or [])
            expected_keywords = spec.get("expected_keywords") or []
            matched = False
            for card in cards:
                if not _keywords_match(_card_text(card), expected_keywords):
                    continue
                for block_id in card.source_block_ids:
                    block = store.load_evidence_block(block_id)
                    if not block:
                        continue
                    block_message_ids = {message.message_id for message in block.messages}
                    if source_message_ids.issubset(block_message_ids):
                        matched = True
                        break
                if matched:
                    break
            checks.append(
                CheckResult(
                    name=f"{prefix}[{i}]",
                    passed=matched,
                    detail=f"keywords={expected_keywords} source_message_ids={sorted(source_message_ids)}",
                )
            )
        return checks
