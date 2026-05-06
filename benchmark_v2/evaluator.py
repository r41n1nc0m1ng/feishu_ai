from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from memory import store
from memory.schemas import CardStatus, MemoryCard, MemoryRelation, TopicSummary


def _normalize_text(value: str) -> str:
    return (value or "").strip().lower()


def _card_text(card: MemoryCard) -> str:
    parts = [
        card.decision_object or "",
        card.decision or "",
        card.reason or "",
        " ".join(card.tentative_consensus or []),
        " ".join(card.open_questions or []),
        " ".join(card.discussion_scope or []),
        card.next_step or "",
    ]
    return " ".join(p for p in parts if p).lower()


def _topic_text(topic: TopicSummary) -> str:
    return f"{topic.topic} {topic.summary}".lower()


def _keywords_match(text: str, expected_keywords: list[str] | None) -> bool:
    """ALL-of：所有关键词都必须出现。空列表视为不约束。"""
    if not expected_keywords:
        return True
    normalized = _normalize_text(text)
    return all(_normalize_text(keyword) in normalized for keyword in expected_keywords)


def _keywords_match_any(text: str, keywords: list[str] | None) -> bool:
    """ANY-of：至少有一个关键词出现。空列表视为不约束。"""
    if not keywords:
        return True
    normalized = _normalize_text(text)
    return any(_normalize_text(keyword) in normalized for keyword in keywords)


def _keywords_absent(text: str, forbidden_keywords: list[str] | None) -> bool:
    if not forbidden_keywords:
        return True
    normalized = _normalize_text(text)
    return all(_normalize_text(keyword) not in normalized for keyword in forbidden_keywords)


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


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

    def skipped_case_eval(self, reason: str) -> EvaluatorSummary:
        return EvaluatorSummary(
            passed=True,
            checks=[CheckResult(name="case_deep_eval", passed=True, detail=f"skipped:{reason}")],
            metrics={"total_checks": 1, "passed_checks": 1, "failed_checks": 0},
        )

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
        checks.extend(self._check_card_relations(chat_id, batch))
        checks.extend(self._check_topics(chat_id, batch))
        checks.extend(self._check_batch_evidence(chat_id, batch))

        passed = all(check.passed for check in checks)
        metrics = {
            "total_checks": len(checks),
            "passed_checks": sum(1 for check in checks if check.passed),
            "failed_checks": sum(1 for check in checks if not check.passed),
        }
        return EvaluatorSummary(passed=passed, checks=checks, metrics=metrics)

    async def evaluate_case(self, case: dict[str, Any]) -> EvaluatorSummary:
        checks: list[CheckResult] = []
        if not self.deep_eval_enabled:
            return self.skipped_case_eval("deep_eval_disabled")

        final_expected = case.get("expected") or {}
        final_memory_checks = final_expected.get("final_memory_checks") or []
        relation_checks = final_expected.get("relation_checks") or []
        evidence_checks = final_expected.get("evidence_checks") or []
        recall_metrics = await self._build_recall_metrics(case, final_memory_checks)
        interference_metrics = self._build_interference_metrics(case)
        conflict_metrics = self._build_conflict_metrics(case)

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
                "recall_metrics": recall_metrics,
                "interference_metrics": interference_metrics,
                "conflict_metrics": conflict_metrics,
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
            expected_keywords          = spec.get("expected_keywords") or []
            expected_keywords_any      = spec.get("expected_keywords_any") or []
            forbidden_keywords         = spec.get("forbidden_keywords") or []
            expected_status            = spec.get("expected_status")
            expected_memory_type       = spec.get("expected_memory_type")
            expected_decision_objects  = _as_list(spec.get("expected_decision_object"))
            forbidden_decision_objects = _as_list(spec.get("forbidden_decision_objects"))
            matched_cards = []
            for card in cards:
                if expected_status and card.status.value != expected_status:
                    continue
                if expected_memory_type and card.memory_type.value != expected_memory_type:
                    continue
                if expected_decision_objects and card.decision_object not in expected_decision_objects:
                    continue
                if forbidden_decision_objects and card.decision_object in forbidden_decision_objects:
                    continue
                text = _card_text(card)
                if not _keywords_match(text, expected_keywords):
                    continue
                if not _keywords_match_any(text, expected_keywords_any):
                    continue
                if not _keywords_absent(text, forbidden_keywords):
                    continue
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
        forbidden_specs = expected_write.get("forbidden_relations") or []
        if not self.deep_eval_enabled:
            if not specs and not forbidden_specs:
                return []
            return [CheckResult(name="relation_eval", passed=True, detail="skipped")]
        checks = self._check_relation_specs({"chat_id": chat_id}, specs, prefix="expected_relations")
        checks.extend(self._check_relation_specs({"chat_id": chat_id}, forbidden_specs, prefix="forbidden_relations"))
        return checks

    def _check_card_relations(self, chat_id: str, batch: dict[str, Any]) -> list[CheckResult]:
        """检查 refine / supersede / progress_complete / progress_refine 的"卡片对"关系。

        与 expected_relations(走 MemoryRelation 表)不同，这里直接读 MemoryCard.supersedes_memory_ids
        字段，能区分 refine（合并卡 supersedes 长度=2）、supersede（长度=1）等不同 operation 语义；
        也能直接拿到合并/被合并卡片的 memory_id 链路，便于审计。

        spec schema:
          {
            "operation": "supersede" | "refine" | "progress_complete" | "progress_refine",
            "head_keywords": ["最新合并/覆盖卡的关键词"],          # 在 head 卡的 _card_text 上 ALL-match
            "head_decision_object": "可选，head 卡的 decision_object 精确匹配",
            "head_status": "active|deprecated（默认 active）",
            "source_keywords": [                                  # 与 head.supersedes_memory_ids 等长
              ["源卡1关键词", ...],
              ["源卡2关键词", ...]
            ]
          }
        """
        expected_write = batch.get("expected_write_result") or {}
        specs = expected_write.get("expected_card_relations") or []
        if not self.deep_eval_enabled:
            if not specs:
                return []
            return [CheckResult(name="card_relations_eval", passed=True, detail="skipped")]

        op_to_count = {
            "supersede": 1,
            "refine": 2,
            "progress_complete": 2,
            "progress_refine": 2,
        }

        cards = store.get_cards_for_chat(chat_id)
        cards_by_id = {c.memory_id: c for c in cards}
        checks: list[CheckResult] = []

        for i, spec in enumerate(specs):
            op = str(spec.get("operation") or "").strip().lower()
            head_kws = spec.get("head_keywords") or []
            head_obj = spec.get("head_decision_object")
            head_status = (spec.get("head_status") or "active").strip().lower()
            source_specs = spec.get("source_keywords") or []
            expected_count = op_to_count.get(op, len(source_specs) or None)

            matched_card = None
            detail = ""
            for card in cards:
                if card.status.value != head_status:
                    continue
                if head_obj and card.decision_object != head_obj:
                    continue
                if not card.supersedes_memory_ids:
                    continue
                if expected_count is not None and len(card.supersedes_memory_ids) != expected_count:
                    continue
                if not _keywords_match(_card_text(card), head_kws):
                    continue

                # 解析每个源卡（先内存后 SQLite）
                source_cards = []
                for sid in card.supersedes_memory_ids:
                    sc = cards_by_id.get(sid) or store.load_memory_card(sid)
                    if sc:
                        source_cards.append(sc)
                if len(source_cards) != len(card.supersedes_memory_ids):
                    continue

                # source_specs 与每张源卡做无序唯一匹配
                if source_specs:
                    used: set[int] = set()
                    all_ok = True
                    for s_kws in source_specs:
                        s_kws = list(s_kws or [])
                        found = False
                        for j, sc in enumerate(source_cards):
                            if j in used:
                                continue
                            if _keywords_match(_card_text(sc), s_kws):
                                used.add(j)
                                found = True
                                break
                        if not found:
                            all_ok = False
                            break
                    if not all_ok:
                        continue

                matched_card = card
                detail = f"head={card.memory_id} sources={list(card.supersedes_memory_ids)}"
                break

            checks.append(
                CheckResult(
                    name=f"expected_card_relations[{i}]",
                    passed=bool(matched_card),
                    detail=detail or f"op={op} head_keywords={head_kws}",
                )
            )

        return checks

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
            forbidden_relation_type = spec.get("forbidden_relation_type")
            old_keywords = spec.get("old_expected_keywords") or []
            new_keywords = spec.get("new_expected_keywords") or []
            matched = False
            forbidden_matched = False
            for relation in relations:
                source_card = cards.get(relation.source_id) or store.load_memory_card(relation.source_id)
                target_card = cards.get(relation.target_id) or store.load_memory_card(relation.target_id)
                if not source_card or not target_card:
                    continue
                if new_keywords and not _keywords_match(_card_text(source_card), new_keywords):
                    continue
                if old_keywords and not _keywords_match(_card_text(target_card), old_keywords):
                    continue
                if relation_type and relation.relation_type.value == relation_type:
                    matched = True
                if forbidden_relation_type and relation.relation_type.value == forbidden_relation_type:
                    forbidden_matched = True
                if matched and (not forbidden_relation_type or forbidden_matched):
                    break
                if forbidden_relation_type and forbidden_matched and not relation_type:
                    break

            if forbidden_relation_type and relation_type:
                passed = matched and not forbidden_matched
                detail = (
                    f"required={relation_type} forbidden={forbidden_relation_type} "
                    f"old={old_keywords} new={new_keywords}"
                )
            elif forbidden_relation_type:
                passed = not forbidden_matched
                detail = (
                    f"forbidden={forbidden_relation_type} "
                    f"old={old_keywords} new={new_keywords}"
                )
            else:
                passed = matched
                detail = f"{relation_type} old={old_keywords} new={new_keywords}"
            checks.append(
                CheckResult(
                    name=f"{prefix}[{i}]",
                    passed=passed,
                    detail=detail,
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

    def _check_batch_evidence(self, chat_id: str, batch: dict[str, Any]) -> list[CheckResult]:
        expected_write = batch.get("expected_write_result") or {}
        specs = expected_write.get("expected_evidence_checks") or []
        if not self.deep_eval_enabled:
            if not specs:
                return []
            return [CheckResult(name="batch_evidence_eval", passed=True, detail="skipped")]
        return self._check_evidence_specs({"chat_id": chat_id}, specs, prefix="expected_evidence_checks")

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
            source_message_ids = set(
                spec.get("expected_source_message_ids")
                or spec.get("source_message_ids")
                or []
            )
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

    async def _build_recall_metrics(
        self,
        case: dict[str, Any],
        specs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not specs:
            return {
                "queries": 0,
                "top1_hits": 0,
                "top3_hits": 0,
                "top1_hit_rate": None,
                "top3_hit_rate": None,
                "avg_retrieval_latency_ms": None,
                "details": [],
            }

        details: list[dict[str, Any]] = []
        top1_hits = 0
        top3_hits = 0
        top5_hits = 0
        first_hit_ranks: list[int] = []
        score_margins: list[float] = []
        latencies: list[float] = []

        for spec in specs:
            chat_id = str(spec.get("chat_id") or case.get("chat_id") or "")
            query = str(spec.get("query") or "")
            target = str(spec.get("expected_granularity") or "memory_card")
            expected_keywords = spec.get("expected_keywords") or []
            forbidden_keywords = spec.get("forbidden_keywords") or []

            started = time.perf_counter()
            ranked = await self._rank_candidates(chat_id, query, target=target)
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            latencies.append(latency_ms)

            matched_rank = None
            for idx, item in enumerate(ranked, 1):
                text = item["text"]
                if _keywords_match(text, expected_keywords) and _keywords_absent(text, forbidden_keywords):
                    matched_rank = idx
                    break

            top1 = matched_rank == 1
            top3 = matched_rank is not None and matched_rank <= 3
            top5 = matched_rank is not None and matched_rank <= 5
            top1_hits += int(top1)
            top3_hits += int(top3)
            top5_hits += int(top5)
            if matched_rank is not None:
                first_hit_ranks.append(matched_rank)
            if len(ranked) >= 2:
                score_margins.append(round(float(ranked[0]["score"]) - float(ranked[1]["score"]), 6))
            elif len(ranked) == 1:
                score_margins.append(round(float(ranked[0]["score"]), 6))
            details.append(
                {
                    "query": query,
                    "target": target,
                    "candidate_count": len(ranked),
                    "matched_rank": matched_rank,
                    "top1_hit": top1,
                    "top3_hit": top3,
                    "top5_hit": top5,
                    "retrieval_latency_ms": latency_ms,
                }
            )

        query_count = len(specs)
        return {
            "queries": query_count,
            "top1_hits": top1_hits,
            "top3_hits": top3_hits,
            "top5_hits": top5_hits,
            "top1_hit_rate": round(top1_hits / query_count, 4) if query_count else None,
            "top3_hit_rate": round(top3_hits / query_count, 4) if query_count else None,
            "top5_hit_rate": round(top5_hits / query_count, 4) if query_count else None,
            "mean_first_hit_rank": round(sum(first_hit_ranks) / len(first_hit_ranks), 3) if first_hit_ranks else None,
            "median_first_hit_rank": round(sorted(first_hit_ranks)[len(first_hit_ranks) // 2], 3) if first_hit_ranks else None,
            "avg_score_margin": round(sum(score_margins) / len(score_margins), 6) if score_margins else None,
            "avg_retrieval_latency_ms": round(sum(latencies) / len(latencies), 3) if latencies else None,
            "details": details,
        }

    def _build_interference_metrics(self, case: dict[str, Any]) -> dict[str, Any]:
        batches = case.get("batches") or []
        total_batches = len(batches)
        noise_batches = 0
        query_batches = 0
        schedule_batches = 0
        task_batches = 0
        cross_group_batches = 0
        parallel_batches = 0
        difficult_batches = 0
        multi_intent_batches = 0
        accepted_noise_ignored = 0
        total_noise_guard_checks = 0

        for batch in batches:
            tags = {str(tag) for tag in (batch.get("tags") or [])}
            expected = batch.get("expected") or {}
            expected_actions = list(expected.get("realtime_actions") or [])
            should_ignore = set((batch.get("expected_write_result") or {}).get("should_ignore_message_ids") or [])
            messages = batch.get("messages") or []
            if "noise" in tags or "anti_interference" in tags or "anti_noise" in tags:
                noise_batches += 1
            if "query" in tags:
                query_batches += 1
            if "schedule" in tags:
                schedule_batches += 1
            if "task" in tags:
                task_batches += 1
            if "multi_group" in tags or "topic_boundary" in tags:
                cross_group_batches += 1
            if "parallel" in tags or "parallel_discussion" in tags or "classification" in tags:
                parallel_batches += 1
            if len(tags & {"noise", "query", "schedule", "task", "parallel", "classification", "multi_group", "topic_boundary"}) >= 2:
                multi_intent_batches += 1
            if len(messages) >= 5 or len(tags & {"parallel", "classification", "multi_group", "topic_boundary"}) > 0:
                difficult_batches += 1

            total_noise_guard_checks += len(messages)
            for idx, action in enumerate(expected_actions):
                if action == "noop" and idx < len(messages):
                    accepted_noise_ignored += 1

            total_expected = len(messages)
            if should_ignore:
                accepted_noise_ignored += len(should_ignore)

        return {
            "batches": total_batches,
            "noise_batches": noise_batches,
            "query_batches": query_batches,
            "schedule_batches": schedule_batches,
            "task_batches": task_batches,
            "cross_group_batches": cross_group_batches,
            "parallel_batches": parallel_batches,
            "difficult_batches": difficult_batches,
            "multi_intent_batches": multi_intent_batches,
            "noise_guard_coverage": round(accepted_noise_ignored / total_noise_guard_checks, 4) if total_noise_guard_checks else None,
        }

    def _build_conflict_metrics(self, case: dict[str, Any]) -> dict[str, Any]:
        batches = case.get("batches") or []
        total_batches = len(batches)
        refine_batches = 0
        supersede_batches = 0
        conflict_batches = 0
        hard_conflict_batches = 0
        relation_expectations = 0
        supersede_expectations = 0
        refine_expectations = 0
        conflict_expectations = 0

        for batch in batches:
            tags = {str(tag) for tag in (batch.get("tags") or [])}
            expected_write = batch.get("expected_write_result") or {}
            relation_specs = expected_write.get("expected_relations") or []
            if "refine" in tags:
                refine_batches += 1
            if "supersede" in tags:
                supersede_batches += 1
            if "conflict" in tags or "supersede_candidate" in tags:
                conflict_batches += 1
            if "supersede" in tags and "conflict" in tags:
                hard_conflict_batches += 1
            relation_expectations += len(relation_specs)
            for spec in relation_specs:
                relation_type = str(spec.get("relation_type") or "")
                if relation_type == "supersedes":
                    supersede_expectations += 1
                elif relation_type == "refines":
                    refine_expectations += 1
                elif relation_type == "contradicts":
                    conflict_expectations += 1

        return {
            "batches": total_batches,
            "refine_batches": refine_batches,
            "supersede_batches": supersede_batches,
            "conflict_batches": conflict_batches,
            "hard_conflict_batches": hard_conflict_batches,
            "relation_expectations": relation_expectations,
            "supersede_expectations": supersede_expectations,
            "refine_expectations": refine_expectations,
            "conflict_expectations": conflict_expectations,
        }

    async def _rank_candidates(self, chat_id: str, query: str, *, target: str) -> list[dict[str, Any]]:
        """走真 production retriever（hybrid + 可选 LLM rerank），让 recall 指标真实反映线上行为。

        score 字段不再是字符重合度，而是按返回顺序的合成单调降序值（1.0 / 0.99 / ...），
        仅用于保留 score_margin 度量结构；hit_rate 类指标只看顺序。
        """
        from memory.retriever import MemoryRetriever
        retriever = MemoryRetriever()
        if target == "topic_summary":
            topics = await retriever.retrieve_topic_summary(chat_id, query, limit=20)
            return [{"score": 1.0 - i * 0.01, "text": _topic_text(t)} for i, t in enumerate(topics)]

        cards = await retriever.retrieve(chat_id, query, limit=20)
        return [{"score": 1.0 - i * 0.01, "text": _card_text(c)} for i, c in enumerate(cards)]
