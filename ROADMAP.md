# OpenClaw Memory 研发路线

## 一、项目定位

OpenClaw Memory 是一个部署在飞书群聊中的决策记忆机器人。核心目标：**自动识别群聊中形成的决策，结构化存储，并在后续讨论中主动召回**，避免团队反复讨论同一问题或按旧决策执行。

系统以「群聊即记忆边界」为设计原则，每个飞书群对应一个独立的 Chat Memory Space，记忆不跨群共享。

---

## 二、当前架构

```
飞书群聊
  │  WebSocket 长连接（lark-oapi SDK）
  ▼
event_handler.py          消息解析 → FeishuMessage
  │
  ▼
ZepSessionManager         短期滑动窗口缓存（in-memory deque, maxlen=30）
  │
  ▼
ContextBuilder            组装上下文：最近10条消息 + memory_hints
  │
  ▼
OpenClawClient            调用 Ollama /api/generate 进行记忆抽取
  │   ├─ 有记忆价值 → ExtractedMemory（title/decision/reason/type/participants）
  │   └─ 无记忆价值 → 丢弃
  ▼
GraphitiClient            写入 Neo4j 时序知识图谱（group_id = chat_id）
  │
  ▼
FeishuAPIClient           向群聊发送文本回复
```

**底层依赖**

| 组件 | 用途 | 当前版本 |
|------|------|---------|
| lark-oapi | 飞书 WebSocket 事件接收 | ≥1.3.0 |
| graphiti-core | 时序知识图谱（记忆存储/检索） | ≥0.3.0 |
| neo4j | Graphiti 持久化后端 | ≥5.19.0 |
| Ollama + qwen2.5:7b | 本地推理（记忆抽取） | — |
| Ollama + nomic-embed-text | 向量嵌入（语义检索） | — |
| httpx | HTTP 客户端（trust_env=False 绕过 Windows 代理） | ≥0.27.0 |

---

## 三、已完成模块

### 3.1 飞书接入层
- `main.py` — asyncio 主循环 + lark WebSocket 长连接，无需公网地址
- `feishu/event_handler.py` — 双入口：`handle_lark_event`（生产）/ `handle_raw_event`（本地测试）
- `feishu/api_client.py` — tenant_access_token 缓存、向群聊发送文本消息

### 3.2 记忆存储层
- `memory/schemas.py` — 完整数据模型：`FeishuMessage` / `ExtractedMemory` / `MemoryItem` / `EventBlock` / `TopicSummary`
- `memory/graphiti_client.py` — Graphiti 初始化（OllamaLLMClient + PassthroughReranker + OpenAIEmbedder）、`add_episode`、`search`
- `memory/zep_session.py` — 短期上下文缓冲（in-memory，接口与 Zep CE 兼容，可后续替换）

### 3.3 记忆抽取层
- `openclaw_bridge/client.py` — 模型参数外置（`EXTRACT_MODEL`），支持 OpenClaw 服务器或回退 Ollama，JSON 格式化输出 + 枚举容错
- `openclaw_bridge/context_builder.py` — 最小化上下文视图（近期消息窗口 + memory hints），避免 context 膨胀

### 3.4 预处理层（骨架已就绪）
- `preprocessor/time_window.py` — 时间窗口批处理器（WINDOW_SECONDS=180 / MAX_WINDOW_MESSAGES=20）
- `preprocessor/keyword_filter.py` — 关键词预过滤（决定/确定/不做/改成 等触发词）
- `preprocessor/lightweight_llm.py` — 轻量 LLM 门控（yes/no 判断批次是否含决策内容，fail-open）

### 3.5 记忆管理层（骨架已就绪）
- `memory/retriever.py` — Graphiti 语义检索封装，`search` / `search_active`
- `memory/conflict_detector.py` — 冲突检测（标题/决策文本重叠启发式，待替换为 LLM 判断）
- `memory/lifecycle.py` — 记忆过期/废弃（接口定义完成，等待 Graphiti 节点更新 API）
- `memory/topic_manager.py` — 高粒度话题聚合（骨架，待实现 episode 聚类）

### 3.6 测试
- `tests/local_test.py` — 端到端本地测试，无需飞书，验证完整处理链路
- `tests/recall_accuracy_test.py` / `anti_noise_test.py` / `conflict_update_test.py` — P0 基准测试骨架

---

## 四、研发路线

### Phase 1 — 预处理层接入（当前优先级最高）

**目标**：将现有的「每条消息立即送 OpenClaw」改为「批处理窗口触发」，减少无效调用、还原真实决策上下文。

**涉及文件**：`preprocessor/time_window.py`、`preprocessor/keyword_filter.py`、`preprocessor/lightweight_llm.py`、`feishu/event_handler.py`

**改动逻辑**：

```
当前流程：
消息 → ZepSession → ContextBuilder → OpenClaw（每条触发）

目标流程：
消息 → ZepSession
      ↓
      KeywordFilter（无触发词 → 仅缓存）
      ↓（有触发词）
      TimeWindowAccumulator（积累消息）
      ↓（超时或达到上限）
      LightweightLLM Gate（yes/no）
      ↓（yes）
      ContextBuilder → OpenClaw
```

**关键决策**：
- 关键词过滤与时间窗口并联（任一命中均可触发积累），而非串联，避免截断讨论
- LLM 门控 fail-open（调用失败时放行，不静默丢弃）
- `event_handler._process()` 改为接收 `EventBlock`（多条消息）而非单条 `FeishuMessage`

---

### Phase 2 — 共识判断与卡片推送

**目标**：实现需求文档第 4.5 节「结果形成与确认推送层」。机器人不再立即回复文字，而是判断讨论是否收束后推送飞书卡片。

**涉及文件**：`openclaw_bridge/skills/consensus_judge.yaml`、`feishu/cards/*.json`、`feishu/event_handler.py`

**共识判断逻辑**：

```
ExtractedMemory 生成后
  ↓
consensus_judge skill（Ollama）
  ├─ 同意信号（同意/OK/就这样）+ 无反对 + 窗口超时 → 一致决策卡片
  ├─ 同意信号 + 话题切换信号 → 一致决策卡片
  ├─ 单人表达，无确认 → 讨论进度卡片
  └─ 出现反对/延伸信号 → 继续观察，不推送
```

**飞书卡片**：

| 卡片文件 | 使用场景 |
|---------|---------|
| `decision_card.json` | 多人确认的一致决策，含「删除」按钮 |
| `progress_card.json` | 讨论进行中，尚未形成一致结论 |
| `conflict_card.json` | 新决策与旧决策冲突，含新旧对比和版本链 |

卡片需实现「删除」交互回调（`card.action` 事件），在反悔窗口内允许成员撤销记录。

---

### Phase 3 — 冲突检测与版本链

**目标**：实现需求文档第 4.8 节「冲突更新」。新决策写入前先检索同话题旧记忆，触发版本覆盖流程。

**涉及文件**：`memory/conflict_detector.py`、`memory/lifecycle.py`、`feishu/event_handler.py`

**流程**：

```
新 ExtractedMemory
  ↓
ConflictDetector.find_conflict(chat_id, new_memory)
  ├─ 无冲突 → 正常写入 Graphiti
  └─ 有冲突 → 推送 conflict_card（展示新旧决策对比）
               ↓ 群内确认后
               lifecycle.deprecate(旧记忆)
               新记忆写入，supersedes 指向旧记忆 ID
```

**冲突判断升级路径**：
- 当前：标题/决策文本重叠启发式（已在骨架中实现）
- 目标：替换为 Ollama 两段文本语义对比（`conflict_detect.md` prompt）

---

### Phase 4 — 历史召回触发

**目标**：实现需求文档第 4.7 节「记忆检索与触发层」。

**两种触发方式**：

1. **`@机器人` 提问**：解析 `@` mention 事件，提取问题文本，调用 `MemoryRetriever.retrieve(chat_id, query)` 返回结果卡片

2. **语义相似自动触发**：群聊消息经 LLM 轻量判断是否为「追问/确认/疑问」语意，若是且与已有记忆高相似度则主动推送，若为普通陈述则静默

**涉及文件**：`openclaw_bridge/skills/recall.yaml`、`memory/retriever.py`、`feishu/event_handler.py`

---

### Phase 5 — 三层记忆架构完善

**目标**：将当前「平铺 episode」升级为三层结构，提升大体量记忆下的检索精度。

```
低粒度  raw episodes      ← 已有（Graphiti add_episode）
  ↓
中粒度  decision summaries ← 已有（ExtractedMemory → episode body）
  ↓ 语义聚类（cosine 相似度 / GMM）
高粒度  topic nodes        ← TopicManager 待实现
```

**`TopicManager.rebuild_topics` 实现步骤**：
1. 从 Graphiti 拉取该 chat_id 下所有 episodes
2. 对 episode summary 做向量嵌入
3. 按余弦相似度聚类（K-means 或 HDBSCAN，K 由信息熵自适应估计）
4. 每个簇用 Ollama 生成 `TopicSummary.topic` + `summary`
5. 写入 Graphiti community node（group_id = chat_id）

召回时优先命中 topic 节点，再展开到下属 episodes，兼顾速度与粒度。

---

### Phase 6 — 信息分流（P1 功能）

**目标**：对非「Memory」类消息启用飞书原生能力，不污染记忆库。

**分类结果对应处理**（`classify.yaml` skill）：

| 分类 | 处理 | 飞书 API |
|------|------|---------|
| `Schedule` | 提取主题/时间/参与者 → 创建日程确认卡片 | 日程 API |
| `Task` | 提取任务/负责人/截止时间 → 创建待办确认卡片 | 任务 API |
| `Memory` | 进入记忆处理流水线 | — |
| `Ignore` | 丢弃，仅短期缓存 | — |

---

## 五、技术债务与已知限制

### 5.1 模型稳定性
- `qwen2.5:7b` 在 Graphiti 内部 Pydantic 模型抽取时字段名偶发错误，已通过 `_build_example()` 注入 JSON 示例缓解，但更大模型（`qwen2.5:14b`）稳定性更好
- 切换模型只需修改 `openclaw_bridge/client.py` 中的 `EXTRACT_MODEL` 和 `.env` 中的 `LOCAL_MODEL`

### 5.2 短期记忆无持久化
- `ZepSessionManager` 基于 in-memory deque，进程重启后丢失
- 正式版可替换为 Redis 或 SQLite，接口已兼容（`ensure_session` / `add_message` / `get_recent_messages`）

### 5.3 记忆生命周期
- `MemoryLifecycle.deprecate()` 当前为占位实现，Graphiti 未暴露直接节点更新 API
- 替代方案：写入一条 `supersedes=旧记忆ID` 的新 episode，Graphiti 时序语义会自动优先返回新事实

### 5.4 飞书卡片交互
- `decision_card.json` 等模板尚为空，需按飞书卡片 DSL 填充
- 「删除」按钮的 `card.action` 回调需在 `feishu/event_handler.py` 中新增处理分支

### 5.5 Windows 代理
- 已全局设置 `trust_env=False` 解决 Windows 系统代理拦截 localhost 的问题，服务器部署后可移除但保留无副作用

---

## 六、P0 验收标准

研发路线以以下三个基准测试为阶段性验收门槛：

| 测试 | 通过标准 | 测试文件 |
|------|---------|---------|
| 召回准确率 | 自然语言提问返回正确决策关键词，耗时 ≤5s | `tests/recall_accuracy_test.py` |
| 抗干扰 | 50条无关消息注入后原决策仍在 top-3 | `tests/anti_noise_test.py` |
| 冲突更新 | V2 决策写入后查询返回 V2 内容而非 V1 | `tests/conflict_update_test.py` |
