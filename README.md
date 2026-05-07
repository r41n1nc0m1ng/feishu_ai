# Feishu Group Memory

基于飞书群聊的轻量级团队决策记忆系统。

本项目面向小型团队在飞书群聊中常见的“历史决策遗忘、决策理由丢失、版本更新混乱”等问题，将机器人定位为群聊中的后台决策记录者。系统不追求记录所有聊天内容，而是聚焦于长期有复用价值的决策、规则、约束、方案取舍和版本更新。

核心原则是：

> 一个飞书群 = 一个独立的 Chat Memory Space。

系统默认不构建企业级全局记忆库，不跨群自动共享记忆，也不默认读取个人私聊和个人文档。

注意：本文仅介绍系统的基本原理和架构，不涉及具体实现细节。具体字段格式请参考其他文档。

## 当前能力

当前主线已经具备以下闭环能力：

- 飞书群消息接入与后台批处理写入；
- Evidence Block -> Memory Card -> Topic Summary 的多粒度记忆链；
- 群内 `@机器人` 查询、来源追问、版本追问、topic list；
- 日程 / 待办即时分流；
- `refine` / `supersede` / `progress_complete` / `progress_refine` 等更新链路；
- 三类 benchmark 入口：端到端回放、专项回放、指标驱动评测；
- 飞书实播 demo 播放器与离线评测报告输出。

当前仍然明确存在的限制：

- 自动评测仍以关键词匹配为主，不能直接等同于严格语义正确率；
- 写入链包含 LLM 生成与更新判断，同一 case 多次运行时结果会有轻微波动；
- `benchmarkv3` 当前能稳定出报告，但不保证每次跑分达标；
- Graphiti 在本地未初始化时会自动降级，相关结果应按降级环境理解。

## 快速开始

建议按下面顺序使用仓库：

1. 激活你自己的项目环境；
2. 用 `benchmark/mock_main.py` 跑一遍完整链路，确认写入和查询能通；
3. 用 `python -m benchmarkv3.runner` 生成指标报告；
4. 需要录屏时，用 `python -m demo.play_feishu_demo ...` 播放飞书群实播 demo。

最常用命令：

```bash
# 先激活你自己的项目环境
# 例如：
# conda activate <your-env>

# 端到端回放（输出 benchmark/result.json + benchmark/evaluation.json）
python benchmark/mock_main.py

# 指标评测（输出 benchmark_v3_latest.json + _detailed.json）
python -m benchmarkv3.runner

# 飞书实播 demo
python -m demo.play_feishu_demo --reset --message-delay 0.15 --batch-pause 1 --hide-role-label

# 启动真实机器人服务
python main.py
```

## 仓库结构

主要目录分工如下：

- `feishu/`
  - 飞书接入层、消息发送与事件解析
- `realtime/`
  - 即时触发、查询路由、日程/待办处理
- `preprocessor/`
  - 事件分块与消息预处理
- `memory/`
  - EvidenceBlock / MemoryCard / TopicSummary、冲突更新、检索、运行时开关
- `benchmark/`
  - 端到端回放、专项回放、demo trace
- `benchmark_v2/`
  - 分层 fixture 与兼容性评测套件
- `benchmarkv3/`
  - 当前主用的指标驱动评测套件
- `demo/`
  - 飞书群实播演示工具

## Benchmark 体系

当前仓库里保留了三类测试 / 评测入口，分别服务于演示、端到端回放和指标评测：

- `benchmark/mock_main.py`
  - 角色：端到端管线回放器。
  - 作用：让 fixture 消息**走完线上完整通路**（实时层 → 写入层 → 可选 Graphiti），尽可能逼近生产环境。
  - 输出：`benchmark/result.json`（每 batch 产出的 MemoryCard、@bot 回复、TopicSummary 终态、卡片版本链）+ `evaluation.json`。
  - 适合：行为级冒烟、demo 录屏前验收、查看机器人在群里的"实际响应"。

- `benchmarkv3/runner.py`
  - 角色：当前主用的指标驱动评测平台。
  - 作用：**绕开实时层**，直接调 `MemoryRetriever`；跑结构化测试用例，输出可对比的多维指标。
  - 测试用例和消息源解耦：`benchmarkv3/full_demo_case.json`（消息）+ `benchmarkv3/test_cases.json`（独立测试集），方便 A/B 修改。
  - 输出：`benchmarkv3/reports/benchmark_v3_latest.json`（指标摘要）+ `benchmark_v3_latest_detailed.json`（每条 query 的 expected/actual 关键词、检索到的 top-N 卡片完整快照、本次产生的矛盾更新链路、抗干扰 batch 各块 `block_type` 详情）。
  - 适合：写入侧 / 检索侧 / 矛盾更新链路的回归对比，做改动前后的指标差。

- `benchmark/special_case/special_case_replay.py`
  - 角色：单流专项回放。
  - 作用：验证 anti-noise、conflict、final query 召回、Graphiti/SQLite fallback 等专项能力。
  - 特点：更贴近“完整历史对话 + 最终追问”的专项测试，不负责通用分层评测。

- `benchmark_v2/`
  - 角色：分层 benchmark 与兼容性评测套件。
  - 作用：承接 source/runtime 分层、按 chat/tag 筛选、轻量评测 + 深度评测、报表输出。
  - 特点：覆盖 chat/tag 维度筛选和 batch 级检查，适合继续扩 benchmark fixture，但当前评测口径与最新写入链不完全一致。

- `demo/play_feishu_demo.py`
  - 角色：飞书实播演示编排器。
  - 作用：把 `benchmark/full_demo_case.json` 快速播放到真实飞书群，同时把同一批消息喂给后端写入链。
  - 适合：录制“群里消息快速出现 + 机器人后续可被询问”的展示视频。

运行命令：

```bash
# 端到端回放（输出 benchmark/result.json + benchmark/evaluation.json）
python benchmark/mock_main.py

# benchmark_v2 分层回放
python -m benchmark.run_suite --suite v2

# 指标评测（输出 benchmark_v3_latest.json + _detailed.json）
python -m benchmarkv3.runner

# 飞书实播 demo
python -m demo.play_feishu_demo --reset --message-delay 0.15 --batch-pause 1 --hide-role-label
```

其中：

- `mock_main` 更适合回答“机器人在完整链路里会怎么响应”；
- `benchmarkv3` 更适合回答“记忆系统当前质量到什么程度”；
- `benchmark_v2` 更适合继续做分层 fixture 和专项回归。

## 演示与交付材料

如果时间有限，推荐优先展示两类材料：

1. 飞书实播 demo
   - 命令：`python -m demo.play_feishu_demo --reset --message-delay 0.15 --batch-pause 1 --hide-role-label`
   - 另起一个终端运行：`python main.py`
   - 播放结束后，在同一群里 `@机器人` 追问 1-2 个歧义较低的问题。

2. 离线评测结果
   - `benchmark/result.json`
   - `benchmark/evaluation.json`
   - `benchmarkv3/reports/benchmark_v3_latest.json`
   - `benchmarkv3/reports/benchmark_v3_latest_detailed.json`

评测结果解读时需要明确两点：

- 当前自动评分仍以关键词匹配为主，不能把分数直接等同于严格语义正确率。
- 写入侧包含 LLM 生成与冲突更新判断，同一 case 多次运行时，卡片数量、`refine` / `add` / `supersede` 细节可能会有轻微波动。



---

## 1. 项目背景

在日常办公、研发协作和小团队项目推进中，大量关键决策并不会以正式文档形式沉淀，而是散落在飞书群聊、临时讨论和任务沟通中。

团队后续推进时经常遇到：

- 忘记之前已经讨论过什么；
- 只记得结论，却忘记当时为什么这么决定；
- 后来方案改了，但有人仍按照旧版本执行；
- 新成员加入后，需要老成员重复解释历史上下文；
- 人工翻群记录成本高，且很容易漏掉关键原因。

因此，本项目希望让机器人成为飞书群中的“决策记忆助手”：在不打扰日常讨论的前提下，后台沉淀群聊中的关键决策，并在需要时准确召回。

---

## 2. 核心方案

系统采用“双通道 + 多粒度记忆”的架构。

### 2.1 双通道

#### 实时通道

用于处理需要即时响应的内容：

- 用户在群内 @机器人；
- 群聊中出现明显历史提问；
- 群聊中出现开会、日程、待办等可立即执行事项。

#### 批处理通道

用于后台沉淀长期记忆：

- 系统每 10 分钟拉取一次群聊增量消息+少量重叠记忆；
- 对增量消息进行 Event Segmentation；
- 将同一事件边界内的聊天记录保存为 Evidence Block；
- 基于 Evidence Block 生成 Memory Card；
- 基于多张 Memory Card 聚合 Topic Summary。

决策类信息默认不实时打断群聊，不推送“检测到决策”的确认卡片。

---

## 3. 多粒度记忆设计

系统采用三层记忆结构。

### 3.1 Evidence Block：低粒度证据层

Evidence Block 保存一段事件边界内的原始聊天记录，附加分段器产出的元信息：

- chat_id / block_id；
- 时间范围 start_time / end_time；
- 消息列表（每条含 message_id / sender_id / sender_name / timestamp / text）；
- block_type：`decision` / `progress` / `noise`，由 LLM 切分器标注，下游 CardGenerator 据此路由 prompt 分支（progress 走 progress 抽取 schema，noise 直接跳过）；
- topic：分段器预标注的"大方向-小方向"议题，CardGenerator 用作 hint；
- one_line_summary：分段器预提炼的一句话摘要，CardGenerator 用作 hint；
- boundary_signal：切分依据（调试用）。

Evidence Block 不负责总结结论，也不直接作为默认回答内容，只作为后续追溯来源的证据层。

示例：

```text
Evidence Block 001

群 ID：chat_xxx
时间范围：10:00-10:07
block_type: decision
topic: 产品规划-项目范围与MVP边界
one_line_summary: MVP 不做企业级记忆，聚焦群聊决策记忆

消息列表：
- A，10:00：我觉得这次不要做企业级记忆了，权限太复杂。
- B，10:02：同意，先聚焦群聊决策记忆，Demo 更清楚。
- C，10:04：OK，那白皮书也按这个方向写。
- D，10:07：那我们接下来讨论 Benchmark 怎么设计。
```

### 3.2 Memory Card：中粒度决策层

Memory Card 是系统默认检索和回答的主要对象。

它基于一个或多个 Evidence Block 生成，记录结构化决策信息：

- 议题 decision_object（"大方向-小方向" 格式，写入前归一化）+ 归一化主键 decision_object_key（候选筛选用）；
- 决策内容 decision；
- 决策理由 reason（无明确理由填"无"）；
- 记忆类型 memory_type：`decision` / `progress` / `tradeoff` / `rule` / `constraint` / `risk` / `version_update`；
- 当前状态 status：`active` / `deprecated`；
- 来源 source_block_ids（一个或多个 EvidenceBlock）；
- 版本关系 supersedes_memory_ids（List[str]）：长度=1 表示 SUPERSEDE（旧卡被覆盖），长度=2 表示 REFINE / PROGRESS_COMPLETE / PROGRESS_REFINE 合并产出（双源 supersede）。

PROGRESS 卡（讨论未收口、不强行写决策）额外携带四个字段：

- tentative_consensus：候选共识（被提出且无人反对的小结论）；
- open_questions：待决议子问题；
- discussion_scope：讨论涉及的具体对象；
- next_step：下一轮要解决什么。

示例（DECISION 卡）：

```text
议题：产品规划-项目范围与MVP边界
决策：MVP 阶段暂不做企业级记忆，优先聚焦群聊决策记忆。
理由：企业级记忆会引入权限、个人文档、私聊和跨群治理问题，容易让 Demo 失焦。
状态：active
memory_type：decision
来源：[Evidence Block 001]
supersedes_memory_ids：[]
```

示例（PROGRESS 卡，讨论未收口）：

```text
议题：技术实现-技术选型
决策：后端语言选型讨论（本轮未收口）
memory_type：progress
状态：active
tentative_consensus：[团队倾向 Python，希望尽快出 Demo]
open_questions：[后端最终选 Python 还是 Go?]
discussion_scope：[Python, Go]
next_step：下周性能压测后再定
```

### 3.3 Topic Summary：高粒度主题摘要层

Topic Summary 由多张相关 Memory Card 聚合生成，用于回答整体方案、当前状态和方向边界类问题。

示例：

```text
当前 MVP 聚焦群聊决策记忆，不做企业级记忆，不做复杂项目空间；个人私聊仅作为查询、确认和转发入口，不作为默认记忆来源。
```

---

## 4. 核心体验

### 4.1 后台沉淀决策

用户在群里正常讨论，机器人默认保持静默。

```text
A：我觉得这次不要做企业级记忆了，权限太复杂。
B：同意，先聚焦群聊决策记忆，Demo 更清楚。
C：OK，那白皮书也按这个方向写。
D：那我们接下来讨论 Benchmark 怎么设计。
```

系统不会立即推送“检测到决策”的卡片，而是在下一次批处理中：

```text
群聊消息
  ↓
Event Segmentation
  ↓
Evidence Block
  ↓
Memory Card
  ↓
写入当前群 Chat Memory Space
```

### 4.2 历史决策召回

用户后续在群里提问：

```text
我们之前为什么不做企业级记忆来着？
```

机器人返回：

```text
根据本群历史决策：

MVP 阶段暂不做企业级记忆，当前聚焦群聊决策记忆。

当时的理由是：企业级记忆会扩大权限、个人文档、私聊和跨群治理边界，不利于比赛 Demo 聚焦；而群聊本身已经提供了天然的协作边界。

状态：生效中
来源：本群历史讨论
```

### 4.3 @机器人即时检索

只要用户 @机器人，即使不是标准疑问句，也会触发检索。

示例：

```text
@机器人 企业级记忆这个事情
@机器人 项目空间
@机器人 我们之前怎么定的？
```

系统会在当前群的 Chat Memory Space 中检索相关 Memory Card、Topic Summary 或 Evidence Block 来源。

### 4.4 来源追溯

如果用户追问：

```text
当时是谁说的？原话在哪？
```

机器人会展开对应 Evidence Block，展示原始消息来源，而不是默认把大量聊天记录直接暴露给用户。

### 4.5 冲突更新

如果后续出现新的讨论：

```text
A：之前说完全不做个人入口，但我觉得可以保留私聊查询入口。
B：同意，个人私聊只用于查询和确认，不作为默认记忆来源。
C：那就这样改。
```

系统会识别这是对旧决策的更新，并生成新版本 Memory Card：

```text
新决策：保留个人私聊入口，但仅用于查询、确认和转发，不作为默认记忆来源。

被覆盖的旧决策：完全不做个人入口。

关系：new_memory supersedes old_memory

状态：新版本 Active，旧版本 Deprecated
```

后续查询时，系统默认返回当前生效版本。

### 4.6 日程与待办分流

日程和待办属于即时执行事项，不进入长期决策记忆流程。

例如：

```text
明天下午 3 点开 Demo 评审会。
```

机器人即时提示：

```text
检测到一个日程：

明天下午 3 点 Demo 评审会。

是否为本群创建日程？
[创建] [忽略]
```

例如：

```text
张三周五前把 Benchmark Report 的抗干扰测试写完。
```

机器人即时提示：

```text
检测到一个待办：

任务：完成 Benchmark Report 的抗干扰测试部分
负责人：张三
截止时间：本周五

[创建待办] [忽略]
```

---

## 5. 系统架构

```text
飞书接入层
  ↓
实时触发层
  ├── @机器人即时检索
  ├── 群内历史提问召回
  └── 日程 / 待办确认
  ↓
批量消息获取层
  ↓
Event Segmentation 事件分块层
  ↓
Evidence Block 证据层
  ↓
Memory Card 决策记忆层
  ↓
Topic Summary 主题摘要层
  ↓
记忆检索与回答层
```

系统不会把整个群的长期历史都塞入 LLM 的单一上下文中。

LLM 在本项目中的定位是：

- 段切器（事件边界判定 + block_type 标注：decision / progress / noise）；
- Memory Card 生成器（从 EvidenceBlock 提炼 decision_object / decision / reason，区分已收口决策与 PROGRESS 进行中讨论）；
- 冲突关系判断器（5 分类 classify_pair：add / refine / supersede / progress_complete / progress_refine）；
- TopicSummary 生成器（多张 Memory Card 聚合归并为大方向主题）；
- 检索 reranker（hybrid 召回的候选 top-K 重排，可选）。

每次调用 LLM 时，系统只注入当前任务所需的上下文：

```text
当前 Evidence Block
  +
相关历史 Memory Card
  +
相关 Topic Summary
  +
输出格式要求
```

LLM provider 走统一优先级：DeepSeek → OpenAI → Ollama，全部带 `temperature=0 / top_p=1 / seed=LLM_SEED` 参数保证可复现。

---

## 6. 核心数据模型

### 6.1 ChatMemorySpace

每个飞书群对应一个独立记忆空间。

```json
{
  "chat_id": "oc_xxx",
  "group_name": "飞书 AI 挑战赛项目群",
  "created_at": "...",
  "last_fetch_at": "..."
}
```

### 6.2 EvidenceBlock

低粒度证据块。

```json
{
  "block_id": "block_001",
  "chat_id": "oc_xxx",
  "start_time": "2026-04-26T10:01:00",
  "end_time": "2026-04-26T10:07:00",
  "messages": [
    {
      "message_id": "msg_001",
      "sender_id": "ou_xxx",
      "sender_name": "A",
      "timestamp": "2026-04-26T10:01:00",
      "text": "我觉得这次不要做企业级记忆了，权限太复杂。"
    }
  ],
  "block_type": "decision",
  "topic": "产品规划-项目范围与MVP边界",
  "one_line_summary": "MVP 不做企业级记忆，聚焦群聊决策记忆",
  "boundary_signal": "话题切换：从需求边界跳到 Benchmark 设计"
}
```

### 6.3 MemoryCard

中粒度决策记忆。DECISION 卡示例：

```json
{
  "memory_id": "mem_001",
  "chat_id": "oc_xxx",
  "decision_object": "产品规划-项目范围与MVP边界",
  "decision_object_key": "产品规划项目范围与mvp边界",
  "decision": "MVP 阶段暂不做企业级记忆，优先聚焦群聊决策记忆。",
  "reason": "企业级记忆会引入权限、个人文档、私聊和跨群治理问题，容易让 Demo 失焦。",
  "memory_type": "decision",
  "status": "active",
  "source_block_ids": ["block_001"],
  "supersedes_memory_ids": [],
  "tentative_consensus": [],
  "open_questions": [],
  "discussion_scope": [],
  "next_step": null
}
```

PROGRESS 卡（讨论未收口）示例：

```json
{
  "memory_id": "mem_002",
  "chat_id": "oc_xxx",
  "decision_object": "技术实现-技术选型",
  "decision_object_key": "技术实现技术选型",
  "decision": "后端语言选型讨论（本轮未收口）",
  "reason": "团队倾向 Python，但需性能压测后再定",
  "memory_type": "progress",
  "status": "active",
  "source_block_ids": ["block_005"],
  "supersedes_memory_ids": [],
  "tentative_consensus": ["团队倾向 Python，希望尽快出 Demo"],
  "open_questions": ["后端最终选 Python 还是 Go?"],
  "discussion_scope": ["Python", "Go"],
  "next_step": "下周性能压测后再定"
}
```

合并卡（REFINE / PROGRESS_COMPLETE / PROGRESS_REFINE）示例 —— `supersedes_memory_ids` 长度=2 表示由两张源卡合并而来，两张源卡均会被置为 `deprecated`：

```json
{
  "memory_id": "mem_003",
  "decision_object": "质量保障-测试与评测方案",
  "decision": "评测标准为等级一致率 + 关键理由覆盖率",
  "memory_type": "decision",
  "status": "active",
  "supersedes_memory_ids": ["mem_002a_progress", "mem_002b_decision"]
}
```

### 6.4 TopicSummary

高粒度主题摘要。

```json
{
  "summary_id": "summary_001",
  "chat_id": "oc_xxx",
  "topic": "MVP 产品边界",
  "summary": "当前 MVP 聚焦群聊决策记忆，不做企业级记忆、不做复杂项目空间，个人私聊仅作为查询和确认入口。",
  "covered_memory_ids": ["mem_001", "mem_002", "mem_003"]
}
```

### 6.5 MemoryRelation

记忆关系。

```text
related_to：相关
refines：补充或细化
supersedes：覆盖旧版本
contradicts：存在冲突但未完成覆盖
```

---

## 7. MVP 功能范围

### P0：核心闭环

目标：证明系统能从群聊中沉淀决策，并能在后续查询时召回。

- 支持 mock 群聊数据输入；
- 按 chat_id 创建独立 Chat Memory Space；
- 按 10 分钟窗口批量处理消息；
- 将消息划分为 Evidence Block；
- 基于 Evidence Block 生成 Memory Card；
- 支持用户提问时检索 Memory Card；
- 支持展开 Evidence Block 查看来源。

完成标准：

```text
输入一段模拟群聊
  ↓
系统生成 Evidence Block
  ↓
系统生成 Memory Card
  ↓
用户提问
  ↓
系统召回正确 Memory Card
  ↓
用户可查看来源 Evidence Block
```

### P1：复赛增强

目标：让系统从“能记住”升级为“能更新、能追溯、能处理多粒度”。

- embedding 语义召回；
- Active / Deprecated 状态管理；
- supersedes 版本更新链；
- Topic Summary 生成；
- query intent 基础粒度路由；
- @机器人即时检索；
- 群内明显历史疑问主动召回；
- 日程 / 待办即时确认。

### P2：决赛增强

目标：升级为研究型多粒度记忆系统。

- GMM 聚类增强；
- MemGAS-style 新旧记忆关联；
- entropy router 多粒度检索路由；
- 多粒度检索 ablation 实验；
- Graphiti-style temporal relation 增强；
- 可视化记忆版本链和主题聚类结果。

---

## 8. Benchmark 评测

项目评测包含四类测试。

### 8.1 抗干扰测试

目标：验证系统是否能在大量无关消息后仍准确召回历史决策。

测试设计：

```text
先形成一条关键决策
  ↓
插入大量无关聊天
  ↓
用户提问历史决策
  ↓
系统召回对应 Memory Card
```

指标：

- Recall@1；
- Answer Accuracy；
- Evidence Accuracy；
- Noise Robustness。

### 8.2 矛盾更新测试

目标：验证系统是否能正确处理新旧决策冲突。

测试设计：

```text
旧决策：完全不做个人入口
  ↓
新决策：保留个人私聊查询入口
  ↓
用户提问：个人私聊入口到底做不做？
  ↓
系统返回新版本，并将旧版本标记为 Deprecated
```

指标：

- Version Accuracy；
- Deprecated Filtering Accuracy；
- Conflict Update Success Rate。

### 8.3 多粒度检索测试

目标：验证系统是否能根据问题选择合适的记忆粒度。

测试设计：

```text
问题 A：我们之前为什么不做企业级记忆？
→ 返回 Memory Card

问题 B：当时是谁说权限复杂？
→ 展开 Evidence Block

问题 C：当前整体 MVP 边界是什么？
→ 返回 Topic Summary
```

指标：

- Granularity Routing Accuracy；
- Evidence Traceability；
- Summary Completeness。

### 8.4 效能指标测试

目标：验证系统是否能减少人工翻找群聊记录的时间成本。

对比：

```text
人工翻找历史决策耗时
vs
系统召回历史决策耗时
```

示例目标：

```text
人工查找平均 3-5 分钟
系统召回平均 5-10 秒
提效 90% 以上
```

---

## 9. 当前开发路线

### 第一阶段：跑通数据流

- 定义基础 schema；
- 准备 mock 群聊数据；
- 实现批量消息读取；
- 实现基础 Evidence Block 划分。

### 第二阶段：跑通记忆生成与查询

- 调用 OpenClaw / LLM 总结 Evidence Block；
- 生成 Memory Card；
- 实现基础检索；
- 支持来源 Evidence Block 展开。

### 第三阶段：加入版本更新与多粒度

- 实现 Active / Deprecated；
- 实现 supersedes 版本链；
- 实现 Topic Summary；
- 实现 query intent 基础路由。

### 第四阶段：打磨复赛 Demo 与 Benchmark

- 构造抗干扰测试；
- 构造冲突更新测试；
- 构造多粒度检索测试；
- 准备 mock replay demo；
- 整理 Benchmark Report。

### 决赛增强

- GMM 聚类；
- MemGAS-style 新旧记忆关联；
- entropy router；
- 多粒度检索对比实验。

---

## 10. 与普通 RAG 的区别

普通企业知识库 RAG 主要回答：

> 文档里有什么？知识在哪里？

本项目更关注：

> 团队当时怎么决定？为什么这么决定？后来有没有改？

因此，本项目不是简单的文档问答系统，而是一个面向飞书群聊协作场景的决策记忆系统。

核心差异包括：

- 以群聊为记忆边界，而不是企业全局知识库；
- 以决策、理由、版本关系为核心，而不是全文检索；
- Evidence Block / Memory Card / Topic Summary 多粒度记忆结构；
- 支持 Active / Deprecated / supersedes 的版本治理；
- 日程和待办走实时工具链，避免污染长期记忆。

---

## 11. 本地运行

推荐的本地验证顺序：

1. 激活你自己的项目环境，并确认 `.env` 已配置；
2. 跑 `python benchmark/mock_main.py`，看 `benchmark/result.json` 和 `benchmark/evaluation.json`；
3. 跑 `python -m benchmarkv3.runner`，看 `benchmarkv3/reports/` 下两份 report；
4. 需要演示时，先运行 `python main.py`，再运行 `python -m demo.play_feishu_demo ...`；
5. 若只想看分层回放或专项回归，再使用 `benchmark_v2` 和 `benchmark/special_case`。

本地运行时需要注意：

- benchmark 报告文件默认会直接刷新仓库里的 `reports/` 目录；
- 若只是临时试跑，建议先备份现有 report，再执行 benchmark；
- 若日志中出现 `Graphiti 未初始化`，说明当前以 SQLite / 本地降级链路为主；
- 若结果与上一次不完全一致，优先检查 LLM 提供方、检索 rerank 和运行时开关。

---

## 12. 项目状态

当前主线已经完成：

```text
P0：Evidence Block → Memory Card → Query 主链路
P1：embedding / 版本链 / Topic Summary / 即时触发
P1.5：冲突更新、检索重排、benchmarkv3、飞书 demo 播放
```

当前仍在继续打磨：

- 飞书真实群接入；
- benchmark fixture 与评测口径收敛；
- 多粒度检索与 rerank 稳定性；
- 决策更新链的实跑一致性；
- 更可控的展示级 demo 与提交材料。
