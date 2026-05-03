"""
语义切分功能测试 — 诊断 + 验收双模式

诊断模式（默认）：
  - 打印 9 条消息两两余弦相似度矩阵
  - 逐步模拟算法执行过程，显示每条消息的判断
  - 对比不同策略（centroid / first-anchor）和不同阈值的切分结果

验收模式（VALIDATE=1）：
  - 断言三段话题被正确切成 3 块

运行：
    conda run -n feishu python scripts/test_semantic_segmenter.py
    conda run -n feishu python scripts/test_semantic_segmenter.py VALIDATE=1
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from memory.schemas import EvidenceMessage, FetchBatch
from preprocessor.event_segmenter import (
    _centroid, _cosine, _embed, _embed_safe, _segment_time, segment_async,
)

# ── 测试消息：3 个独立话题，快速连续发送 ────────────────────────────────────
BASE = datetime(2026, 5, 3, 10, 0, 0, tzinfo=timezone.utc)
MESSAGES_TEXT = [
    # 话题 A：后端语言选择
    "我们用 Python 还是 Go 写后端",
    "Go 性能好但学习成本高，还是用 Python 更快落地",
    "定了，用 Python",
    # 话题 B：代码评审规则
    "代码评审谁来主导",
    "还是按模块负责人来，各自 review 自己领域的 PR",
    "好，就这个规则",
    # 话题 C：发版截止日期
    "第一版什么时候发",
    "月底之前要有可演示版本",
    "那就定 5 月 28 日作为 demo deadline",
]
EXPECTED_BLOCKS = 3   # 期望切成 3 块


def make_messages(interval_seconds: int = 15) -> list[EvidenceMessage]:
    return [
        EvidenceMessage(
            message_id=f"m{i}",
            sender_id="u1",
            sender_name="测试用户",
            timestamp=BASE + timedelta(seconds=i * interval_seconds),
            text=t,
        )
        for i, t in enumerate(MESSAGES_TEXT)
    ]


def make_batch(interval_seconds: int = 15) -> FetchBatch:
    msgs = make_messages(interval_seconds)
    return FetchBatch(
        chat_id="test_semantic",
        fetch_start=msgs[0].timestamp,
        fetch_end=msgs[-1].timestamp,
        messages=msgs,
    )


# ── 相似度矩阵 ────────────────────────────────────────────────────────────────

async def print_similarity_matrix(embeddings: list[list[float]]) -> None:
    n = len(embeddings)
    print("\n相似度矩阵（行×列 = 两条消息余弦相似度）：")
    header = "     " + "".join(f"  m{i:<3}" for i in range(n))
    print(header)
    for i in range(n):
        row = f"m{i:<3} "
        for j in range(n):
            sim = _cosine(embeddings[i], embeddings[j])
            row += f" {sim:.2f} "
        print(row)


# ── 逐步模拟：centroid 策略 ───────────────────────────────────────────────────

async def simulate_centroid(
    embeddings: list[list[float]],
    threshold: float,
    min_block: int,
) -> list[list[int]]:
    """返回每个 block 包含的消息 index 列表。"""
    print(f"\n-- centroid 策略  threshold={threshold}  min_block={min_block} --")
    blocks: list[list[int]] = []
    current: list[int] = []
    current_embs: list[list[float]] = []

    for i, emb in enumerate(embeddings):
        if not current:
            current.append(i)
            current_embs.append(emb)
            continue

        sim = _cosine(emb, _centroid(current_embs)) if current_embs else 1.0
        can_cut = len(current) >= min_block
        cut = can_cut and sim < threshold

        flag = ">>> CUT" if cut else f"sim={sim:.3f}"
        print(f"  m{i} [{MESSAGES_TEXT[i][:28]:<28}] {flag}")

        if cut:
            blocks.append(current[:])
            current = [i]
            current_embs = [emb]
        else:
            current.append(i)
            current_embs.append(emb)

    if current:
        blocks.append(current)

    print(f"  → {len(blocks)} 块: {blocks}")
    return blocks


# ── 逐步模拟：first-anchor 策略 ───────────────────────────────────────────────

async def simulate_first_anchor(
    embeddings: list[list[float]],
    threshold: float,
    min_block: int,
) -> list[list[int]]:
    """与 centroid 策略的区别：用 block 第一条消息的 embedding 作为锚点。"""
    print(f"\n-- first-anchor 策略  threshold={threshold}  min_block={min_block} --")
    blocks: list[list[int]] = []
    current: list[int] = []
    anchor_emb: list[float] = []

    for i, emb in enumerate(embeddings):
        if not current:
            current.append(i)
            anchor_emb = emb
            continue

        sim = _cosine(emb, anchor_emb) if anchor_emb else 1.0
        can_cut = len(current) >= min_block
        cut = can_cut and sim < threshold

        flag = ">>> CUT" if cut else f"sim={sim:.3f}"
        print(f"  m{i} [{MESSAGES_TEXT[i][:28]:<28}] {flag}")

        if cut:
            blocks.append(current[:])
            current = [i]
            anchor_emb = emb
        else:
            current.append(i)

    if current:
        blocks.append(current)

    print(f"  → {len(blocks)} 块: {blocks}")
    return blocks


# ── 主流程 ────────────────────────────────────────────────────────────────────

async def main(validate: bool = False) -> None:
    print("=" * 60)
    print("Step 1: 获取 embedding")
    print("=" * 60)
    embeddings: list[list[float]] = []
    for i, text in enumerate(MESSAGES_TEXT):
        emb = await _embed(text)
        embeddings.append(emb)
        print(f"  m{i} [{text[:30]}] dim={len(emb)}")

    await print_similarity_matrix(embeddings)

    print("\n" + "=" * 60)
    print("Step 2: 相邻消息相似度（话题边界参考）")
    print("=" * 60)
    for i in range(1, len(embeddings)):
        sim = _cosine(embeddings[i], embeddings[i - 1])
        boundary = " ← 话题边界?" if (i in (3, 6)) else ""
        print(f"  m{i-1}→m{i}: {sim:.3f}{boundary}")

    print("\n" + "=" * 60)
    print("Step 3: 不同参数下的 centroid 策略模拟")
    print("=" * 60)
    for thresh in (0.60, 0.50, 0.45, 0.40):
        for min_b in (2, 3):
            await simulate_centroid(embeddings, thresh, min_b)

    print("\n" + "=" * 60)
    print("Step 4: first-anchor 策略模拟")
    print("=" * 60)
    for thresh in (0.60, 0.55, 0.50, 0.45):
        for min_b in (2, 3):
            await simulate_first_anchor(embeddings, thresh, min_b)

    print("\n" + "=" * 60)
    print("Step 5: 当前 segment_async 实际运行结果")
    print("=" * 60)
    import preprocessor.event_segmenter as _seg
    print(f"  模块常量 SEMANTIC_THRESHOLD={_seg.SEMANTIC_THRESHOLD}")
    print(f"  模块常量 MIN_BLOCK_MESSAGES={_seg.MIN_BLOCK_MESSAGES}")
    print(f"  模块常量 BLOCK_GAP_SECONDS={_seg.BLOCK_GAP_SECONDS}")
    # 消息间隔 15s，强制设定已验证的最优参数，不依赖 .env 配置
    with patch_env("SEGMENTER_STRATEGY", "semantic"), \
         patch_env("SEMANTIC_THRESHOLD", "0.60"), \
         patch_env("MIN_BLOCK_MESSAGES", "3"):
        # 常量在模块加载时固化，需要直接写回以覆盖已缓存的值
        _seg.SEMANTIC_THRESHOLD = 0.60
        _seg.MIN_BLOCK_MESSAGES = 3
        blocks = await segment_async(make_batch(interval_seconds=15))
    print(f"当前配置 → {len(blocks)} 块")
    for b in blocks:
        print(f"  [{b.messages[0].text[:25]} ... {b.messages[-1].text[:25]}]"
              f"  ({len(b.messages)} 条)")

    print("\n" + "=" * 60)
    print("Step 6: 时间切分结果（对照组）")
    print("=" * 60)
    time_blocks = _segment_time(make_batch(interval_seconds=15))
    print(f"时间切分（interval=15s，gap={os.getenv('BLOCK_GAP_SECONDS',300)}s）→ {len(time_blocks)} 块")

    if validate:
        print("\n" + "=" * 60)
        print("验收断言")
        print("=" * 60)
        assert len(blocks) == EXPECTED_BLOCKS, (
            f"期望 {EXPECTED_BLOCKS} 块，实际 {len(blocks)} 块。"
            f"请根据上方诊断结果调整阈值或切换策略。"
        )
        print(f"PASS: 正确切成 {EXPECTED_BLOCKS} 块")


class patch_env:
    """临时设置环境变量的上下文管理器。"""
    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value
        self.old = None

    def __enter__(self):
        self.old = os.environ.get(self.key)
        os.environ[self.key] = self.value
        return self

    def __exit__(self, *_):
        if self.old is None:
            os.environ.pop(self.key, None)
        else:
            os.environ[self.key] = self.old


if __name__ == "__main__":
    validate = len(sys.argv) > 1 and sys.argv[1].startswith("VALIDATE")
    asyncio.run(main(validate=validate))
