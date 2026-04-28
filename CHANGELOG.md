# Changelog

## 批处理写入侧 P0 实现（2026-04-29）

### 新增文件

| 文件 | 功能 |
|------|------|
| `memory/store.py` | SQLite 持久化层，建立 `evidence_blocks` / `memory_cards` / `chat_spaces` 三张表，进程重启后自动恢复所有内存缓存 |
| `memory/evidence_store.py` | EvidenceBlock 三写策略（内存缓存 + SQLite + Graphiti），支持按 block_id 精确查询，启动时从 SQLite 恢复 |
| `memory/card_generator.py` | EvidenceBlock → LLM → MemoryCard，支持 ADD / PROGRESS / SUPERSEDE / NOOP 四种操作，SUPERSEDE 自动标记旧卡片为 Deprecated |
| `memory/batch_processor.py` | 批处理主调度器，`register_chat()` 作为 dispatcher 钩子发现新群聊，`run()` 每 60 秒轮询所有已注册群，`process_now()` 供 bot 入群后立即消化历史消息 |
| `preprocessor/event_segmenter.py` | 离线事件切分，按 5 分钟时间间隔和 30 条消息数量双阈值将 FetchBatch 切分为 List[EvidenceBlock] |
| `CHANGELOG.md` | 本文件 |

### 更新文件

| 文件 | 变更内容 |
|------|---------|
| `memory/schemas.py` | 新增 `EvidenceBlock` / `EvidenceMessage` / `MemoryCard` / `FetchBatch` / `ChatMemorySpace` / `MemoryRelation` / `CardOperation` / `MemoryRelationType` / `TopicSummary`；`FeishuMessage` 补充 `chat_type` / `mentions` / `is_at_bot` |
| `memory/retriever.py` | `retrieve()` 新增 `_find_card_for_fact()` 字符级 Jaccard 匹配，将 Graphiti 搜索结果映射到缓存中真实 MemoryCard（含 `source_block_ids`）；`expand_evidence()` 和 `get_card_by_id()` 支持 SQLite 二级查询 |
| `feishu/api_client.py` | 新增 `fetch_messages()`（增量拉取 + 四道过滤 + 游标设计）、`get_chat_info()`、`get_user_name()`（昵称解析含进程级缓存）、`extract_open_id()`（兼容字符串/dict/SDK 对象三种 mention 格式）；`fetch_messages` 返回 `(messages, last_raw_ts)` 元组，游标基于原始消息时间戳而非过滤后的最后一条 |
| `feishu/event_handler.py` | 重构 `_extract_mentions_from_content` / `_extract_mentions_from_sdk_message` 使用统一的 `extract_open_id()`；`handle_legacy_ingest` 改为调用 `BatchProcessor.register_chat` |
| `main.py` | 新增 `on_bot_added` 处理 `im.chat.member.bot.added_v1` 事件；bot 入群后立即执行 `register_chat_by_id + process_now`；启动 `BatchProcessor().run()` 后台任务 |
| `memory/batch_processor.py` | 游标更新使用 `last_raw_ts`（含被过滤消息的时间戳），解决全部消息被过滤时游标不推进的问题；`_restore_active_chats` 移入 `__init__` 并加幂等标志，防止测试触发 |
| `.gitignore` | 新增 `memory_store.db` |
| `.env.example` | 新增 `FEISHU_BOT_OPEN_ID` |

---

### 写入侧实现效果

```
机器人入群
  ↓ on_bot_added → register_chat_by_id + process_now（立即处理历史消息）
  ↓
群聊消息进入系统（WebSocket 实时通道）
  ↓ dispatch_message → noop → register_chat（注册到活跃群聊表 + SQLite）
  ↓
BatchProcessor.run()（每 60 秒）
  ↓ feishu API 拉取增量消息
    过滤：机器人回复 / @查询消息 / 疑问句
    游标推进至所有原始消息最后时间戳（含被过滤的）
  ↓ event_segmenter.segment() → List[EvidenceBlock]
  ↓ EvidenceStore.save() → 内存缓存 + SQLite + Graphiti episode
  ↓ CardGenerator.generate() → LLM → MemoryCard
      ADD / PROGRESS / SUPERSEDE / NOOP
  ↓ MemoryCard 写入内存缓存 + SQLite + Graphiti episode
  ↓
用户 @机器人 查询
  ↓ RealtimeQueryHandler.retrieve()
      Graphiti 语义搜索 → _find_card_for_fact() 匹配真实 MemoryCard
  ↓ 回复决策内容（含 source_block_ids）
  ↓ 用户追问来源 → expand_evidence() → EvidenceBlock 原始消息
```

**重启持久化**：`memory_store.db` 保存所有 EvidenceBlock、MemoryCard、ChatMemorySpace，重启后三个缓存自动恢复，无需等待新消息注册群聊即可开始轮询。

---

### 测试通过率

| 测试文件 | 用例数 | 结果 |
|---------|-------|------|
| `test_event_segmenter.py` | 8 | ✅ 全部通过 |
| `test_evidence_store.py` | 6 | ✅ 全部通过 |
| `test_card_generator.py` | 6 | ✅ 全部通过 |
| `test_batch_processor.py` | 10 | ✅ 全部通过 |
| `test_retriever_batch.py` | 6 | ✅ 全部通过 |
| `test_p0_functional.py` | 11 | ✅ 全部通过 |
| 队友测试（realtime 模块）| 若干 | ✅ 全部通过 |

---

### Bug 修复记录

| Bug | 修复方式 |
|-----|---------|
| 时区显示为 UTC（应为本地时间）| `datetime.fromtimestamp(ts, tz=utc)` → `datetime.fromtimestamp(ts)`；`replace(tzinfo=utc)` → `astimezone(utc)` |
| 飞书 API `container_id_type` 参数名错误 | `container_type` → `container_id_type` |
| 游标闭区间导致最后一条消息重复拉取 | 客户端按毫秒精度过滤 `timestamp > last_fetch_at` |
| 查询消息/机器人回复被存入 Graphiti | `fetch_messages` 四道过滤（sender_type/mentions/正则）在 body 解析前执行 |
| `mentions` 格式不一致导致 `is_at_bot` 永远为 False | 统一 `extract_open_id()` 兼容字符串、嵌套 dict、SDK 对象 |
| JSON body 空内容/空白字符串解析报错 | `raw_content.strip() or "{}"` 替代 `content or "{}"` |
| 测试写入生产 SQLite 导致启动时恢复 10 个假群聊 | 测试 mock `store.save/load`；`_restore_active_chats` 移入 `__init__` 加幂等标志 |
| 全部消息被过滤时游标不推进 | `fetch_messages` 返回 `last_raw_ts`，无论是否有有效消息都推进游标 |
| `FEISHU_BOT_OPEN_ID` 未填写时 `@机器人` 无法触发查询 | 修复 `extract_open_id` 正确解析嵌套 id 字段；`_is_at_bot` fallback 到 `bool(mentions)` |
