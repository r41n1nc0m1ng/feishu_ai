# Feishu Openclaw Memory

基于飞书群聊与 OpenClaw 的轻量级团队决策记忆系统。

本项目面向小型团队在飞书群聊中常见的“历史决策遗忘、决策理由丢失、版本更新混乱”等问题，将机器人定位为群聊中的后台决策记录者。系统不追求记录所有聊天内容，而是聚焦于长期有复用价值的决策、规则、约束、方案取舍和版本更新。

核心原则是：

> 一个飞书群 = 一个独立的 Chat Memory Space。

系统默认不构建企业级全局记忆库，不跨群自动共享记忆，也不默认读取个人私聊和个人文档。

注意：本文仅介绍系统的基本原理和架构，不涉及具体实现细节。具体字段格式请参考其他文档。



---

## 1. 项目背景

在日常办公、研发协作和小团队项目推进中，大量关键决策并不会以正式文档形式沉淀，而是散落在飞书群聊、临时讨论和任务沟通中。

团队后续推进时经常遇到：

- 忘记之前已经讨论过什么；
- 只记得结论，却忘记当时为什么这么决定；
- 后来方案改了，但有人仍按照旧版本执行；
- 新成员加入后，需要老成员重复解释历史上下文；
- 人工翻群记录成本高，且很容易漏掉关键原因。

因此，本项目希望让 OpenClaw 成为飞书群中的“决策记忆助手”：在不打扰日常讨论的前提下，后台沉淀群聊中的关键决策，并在需要时准确召回。

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

Evidence Block 保存一段事件边界内的原始聊天记录，包括：

- 消息发送人；
- 发送时间；
- 消息内容；
- message_id；
- chat_id。

Evidence Block 不负责总结结论，也不直接作为默认回答内容，只作为后续追溯来源的证据层。

示例：

```text
Evidence Block 001

群 ID：chat_xxx
时间范围：10:00-10:07

消息列表：
- A，10:00：我觉得这次不要做企业级记忆了，权限太复杂。
- B，10:02：同意，先聚焦群聊决策记忆，Demo 更清楚。
- C，10:04：OK，那白皮书也按这个方向写。
- D，10:07：那我们接下来讨论 Benchmark 怎么设计。
```

### 3.2 Memory Card：中粒度决策层

Memory Card 是系统默认检索和回答的主要对象。

它基于一个或多个 Evidence Block 生成，记录结构化决策信息：

- 决策对象；
- 决策内容；
- 决策理由；
- 记忆类型；
- 当前状态；
- 来源 Evidence Block；
- 版本关系。

示例：

```text
决策：MVP 阶段暂不做企业级记忆，优先聚焦群聊决策记忆。

理由：企业级记忆会引入权限、个人文档、私聊和跨群治理问题，容易让 Demo 失焦。

状态：Active

来源：Evidence Block 001
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

系统不会把整个群的长期历史都塞入 OpenClaw 的单一上下文中。

OpenClaw 在本项目中的定位是：

- Evidence Block 总结器；
- Memory Card 生成器；
- 冲突关系判断器；
- Topic Summary 生成器；
- 历史问题回答器。

每次调用 OpenClaw 时，系统只注入当前任务所需的上下文：

```text
当前 Evidence Block
  +
相关历史 Memory Card
  +
相关 Topic Summary
  +
输出格式要求
```

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
      "sender_name": "A",
      "timestamp": "2026-04-26T10:01:00",
      "text": "我觉得这次不要做企业级记忆了，权限太复杂。"
    }
  ]
}
```

### 6.3 MemoryCard

中粒度决策记忆。

```json
{
  "memory_id": "mem_001",
  "chat_id": "oc_xxx",
  "decision_object": "企业级记忆是否进入 MVP",
  "title": "MVP 阶段不做企业级记忆",
  "decision": "MVP 阶段暂不做企业级记忆，优先聚焦群聊决策记忆。",
  "reason": "企业级记忆会引入权限、个人文档、私聊和跨群治理问题，容易让 Demo 失焦。",
  "memory_type": "decision",
  "status": "active",
  "source_block_ids": ["block_001"],
  "related_memory_ids": [],
  "supersedes_memory_id": null
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

当前项目仍处于快速开发阶段。建议优先通过 mock 数据跑通核心链路。

后续计划提供：

```text
scripts/run_mock_demo.py
```

用于一键演示：

```text
mock 群聊输入
  ↓
Evidence Block
  ↓
Memory Card
  ↓
用户查询
  ↓
返回历史决策
  ↓
展开来源证据
  ↓
冲突更新测试
```

---

## 12. 项目状态

当前优先级：

```text
P0：Evidence Block → Memory Card → Query 主链路
P1：embedding / 版本链 / Topic Summary / 即时触发
P2：GMM / MemGAS-style 关联 / entropy router
```

本仓库后续将持续完善：

- 飞书真实群接入；
- mock replay 测试数据；
- Benchmark 测试脚本；
- 多粒度检索评测；
- 决策版本链可视化。