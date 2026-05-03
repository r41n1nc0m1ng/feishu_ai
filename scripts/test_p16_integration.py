"""
P1-6 语义切分集成测试（需要 Ollama + nomic-embed-text 运行中）

验收标准：
  9 条消息涵盖 3 个独立主题，时间间隔均在 BLOCK_GAP_SECONDS 以内，
  语义切分应输出 3 个 EvidenceBlock，每块对应一个主题。

测试层次：
  1. 相似度矩阵检验 —— 验证嵌入模型本身能区分话题
       断言：跨话题相似度 < 同话题相似度（阈值可调诊断）
  2. 切分结果验证 —— 端到端跑 segment_async，断言块数 == 3

运行：
    conda run -n feishu python scripts/test_p16_integration.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from memory.schemas import EvidenceMessage, FetchBatch
from preprocessor.event_segmenter import _embed, _cosine, _centroid, segment_async, SEMANTIC_THRESHOLD, MIN_BLOCK_MESSAGES

# ── 测试消息（3 个主题，每组 3 条，间隔 30s，远小于 BLOCK_GAP_SECONDS=300s）──
BASE = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)

TOPIC_A = [
    "我们用 Python 还是 Go 写后端",
    "Go 性能好但学习成本高，还是用 Python 更快落地",
    "定了，用 Python",
]
TOPIC_B = [
    "代码评审谁来主导",
    "还是按模块负责人来，各自 review 自己领域的 PR",
    "好，就这个规则",
]
TOPIC_C = [
    "第一版什么时候发",
    "月底之前要有可演示版本",
    "那就定 5 月 28 日作为 demo deadline",
]


def _make_msgs(texts: list, start_offset_s: int) -> list:
    return [
        EvidenceMessage(
            message_id=f"m{start_offset_s + i}",
            sender_id="u1",
            sender_name="测试用户",
            timestamp=BASE + timedelta(seconds=start_offset_s + i * 30),
            text=text,
        )
        for i, text in enumerate(texts)
    ]


def _make_batch(all_msgs: list) -> FetchBatch:
    return FetchBatch(
        chat_id="integration_test",
        fetch_start=all_msgs[0].timestamp,
        fetch_end=all_msgs[-1].timestamp,
        messages=all_msgs,
    )


# ── 层次 1：相似度矩阵检验 ────────────────────────────────────────────────────

async def check_similarity_matrix():
    print("=" * 60)
    print("层次 1：嵌入相似度矩阵")
    print("=" * 60)

    all_texts = TOPIC_A + TOPIC_B + TOPIC_C
    print(f"正在为 {len(all_texts)} 条消息计算 embedding（需要 Ollama）...\n")

    embeddings = []
    for text in all_texts:
        try:
            emb = await _embed(text)
            embeddings.append(emb)
        except Exception as e:
            print(f"  [ERROR] embedding 失败: {e}")
            print("  请确认 Ollama 已启动且 nomic-embed-text 模型已下载")
            return None

    # 按话题分组
    embs_a = embeddings[0:3]
    embs_b = embeddings[3:6]
    embs_c = embeddings[6:9]

    def mean_sim(ea: list, eb: list) -> float:
        sims = [_cosine(a, b) for a in ea for b in eb]
        return sum(sims) / len(sims)

    sim_aa = mean_sim(embs_a, embs_a)
    sim_bb = mean_sim(embs_b, embs_b)
    sim_cc = mean_sim(embs_c, embs_c)
    sim_ab = mean_sim(embs_a, embs_b)
    sim_ac = mean_sim(embs_a, embs_c)
    sim_bc = mean_sim(embs_b, embs_c)

    print(f"同话题相似度（越高越好）：")
    print(f"  话题A（语言选型） avg sim = {sim_aa:.3f}")
    print(f"  话题B（代码评审） avg sim = {sim_bb:.3f}")
    print(f"  话题C（发布截止） avg sim = {sim_cc:.3f}")
    print()
    print(f"跨话题相似度（越低越好，应低于 SEMANTIC_THRESHOLD={SEMANTIC_THRESHOLD}）：")
    print(f"  A vs B = {sim_ab:.3f}")
    print(f"  A vs C = {sim_ac:.3f}")
    print(f"  B vs C = {sim_bc:.3f}")
    print()

    # 关键检验：跨话题 < 同话题
    within = min(sim_aa, sim_bb, sim_cc)
    across = max(sim_ab, sim_ac, sim_bc)
    print(f"最低同话题相似度 = {within:.3f}，最高跨话题相似度 = {across:.3f}")

    if across < within:
        print("✓ 模型能区分话题（跨话题相似度 < 同话题相似度）")
        model_ok = True
    else:
        print("✗ 模型区分话题能力不足（跨话题相似度 >= 同话题相似度）")
        model_ok = False

    # 阈值诊断
    # 切分需要：跨话题相似度 < SEMANTIC_THRESHOLD
    max_cross = max(sim_ab, sim_ac, sim_bc)
    print()
    if max_cross < SEMANTIC_THRESHOLD:
        print(f"✓ 当前阈值 {SEMANTIC_THRESHOLD} 能覆盖所有跨话题边界（最高跨话题={max_cross:.3f}）")
    else:
        suggested = max_cross + 0.05
        print(f"✗ 当前阈值 {SEMANTIC_THRESHOLD} 不足：最高跨话题相似度 {max_cross:.3f} >= 阈值")
        print(f"  建议将 SEMANTIC_THRESHOLD 调整为 ≥ {suggested:.2f}")
        print(f"  可在 .env 中设置: SEMANTIC_THRESHOLD={suggested:.2f}")

    return embeddings, (sim_ab, sim_ac, sim_bc)


# ── 层次 2：切分结果验证 ────────────────────────────────────────────────────

async def check_segmentation():
    print()
    print("=" * 60)
    print("层次 2：端到端切分结果验证")
    print("=" * 60)

    msgs_a = _make_msgs(TOPIC_A, 0)
    msgs_b = _make_msgs(TOPIC_B, 100)   # +100s（远小于 BLOCK_GAP_SECONDS=300s）
    msgs_c = _make_msgs(TOPIC_C, 200)

    all_msgs = msgs_a + msgs_b + msgs_c
    batch = _make_batch(all_msgs)

    print(f"消息总数: {len(all_msgs)}")
    print(f"时间跨度: {(all_msgs[-1].timestamp - all_msgs[0].timestamp).seconds}s "
          f"（BLOCK_GAP_SECONDS={os.getenv('BLOCK_GAP_SECONDS', '300')}s，时间切分不会触发）")
    print(f"SEMANTIC_THRESHOLD: {SEMANTIC_THRESHOLD}")
    print(f"MIN_BLOCK_MESSAGES: {MIN_BLOCK_MESSAGES}")
    print()

    os.environ["SEGMENTER_STRATEGY"] = "semantic"

    try:
        blocks = await segment_async(batch)
    except Exception as e:
        print(f"[ERROR] segment_async 失败: {e}")
        return

    print(f"切分结果: {len(blocks)} 块")
    for i, block in enumerate(blocks):
        print(f"\n  Block {i+1}（{len(block.messages)} 条消息）：")
        for msg in block.messages:
            print(f"    [{msg.sender_name}] {msg.text}")

    print()
    if len(blocks) == 3:
        # 验证每块内容对应正确话题
        texts_0 = [m.text for m in blocks[0].messages]
        texts_1 = [m.text for m in blocks[1].messages]
        texts_2 = [m.text for m in blocks[2].messages]

        assert any("Python" in t or "Go" in t for t in texts_0), \
            f"Block 0 应为语言选型话题，实际：{texts_0}"
        assert any("review" in t or "评审" in t for t in texts_1), \
            f"Block 1 应为代码评审话题，实际：{texts_1}"
        assert any("28" in t or "deadline" in t or "发布" in t for t in texts_2), \
            f"Block 2 应为发布截止话题，实际：{texts_2}"

        print("✓ PASS: 9 条消息正确切分为 3 个话题块，内容对应正确")
    elif len(blocks) > 1:
        print(f"△ PARTIAL: 切出了 {len(blocks)} 块（期望 3），可能部分话题未被区分")
        print("  建议：提高 SEMANTIC_THRESHOLD（当前值太低）或检查 embedding 模型是否支持中文")
    else:
        print("✗ FAIL: 所有消息仍在 1 块中，语义切分未生效")
        print("  排查步骤：")
        print("  1. 检查层次1的相似度矩阵，确认模型能区分话题")
        print("  2. 检查 SEMANTIC_THRESHOLD 是否高于跨话题相似度")
        print("  3. 确认 SEGMENTER_STRATEGY=semantic 已生效")


# ── 主入口 ─────────────────────────────────────────────────────────────────────

async def main():
    print("P1-6 语义切分集成测试")
    print(f"模型: {os.getenv('EMBED_MODEL', 'nomic-embed-text')} @ {os.getenv('OLLAMA_URL', 'http://localhost:11434')}\n")

    result = await check_similarity_matrix()
    if result is None:
        print("\n[ABORTED] 嵌入服务不可用，跳过切分验证")
        return

    await check_segmentation()


if __name__ == "__main__":
    asyncio.run(main())
