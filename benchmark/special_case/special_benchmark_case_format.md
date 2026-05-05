# 专项测试集字段格式说明

本文档说明当前 benchmark 专项测试集的 JSON 字段格式。当前专项测试包括：

- 抗干扰测试：`anti_noise`
- 矛盾更新测试：`conflict_supersede` / `conflict_false_positive`
- 准确率测试：`accuracy_memory_card` / `accuracy_evidence_block` / `accuracy_topic_summary`

当前测试集统一采用 **single stream** 格式：

```text
完整历史对话 messages
  ↓
系统自行完成切分 / 记忆抽取 / 版本关系判断 / 聚合摘要
  ↓
最后统一发送 final_query_messages
  ↓
根据 expected 中的预期答案进行规则校验或 LLM 语义打分
```

也就是说，测试集本身 **不再预先切分 batch**。batch 切分、Event Segmentation、MemoryCard 生成、TopicSummary 聚合等过程都交给被测系统完成。

---

## 1. 顶层结构

每个 case 是一个独立 JSON 文件，基本结构如下：

```json
{
  "schema_version": "anti_noise_single_stream_v1",
  "case_id": "anti_noise_001_light",
  "description": "测试场景说明",
  "chat_id": "oc_demo_xxx",
  "test_type": "anti_noise",
  "replay_policy": {},
  "messages": [],
  "final_query_messages": [],
  "expected": {}
}
```

| 字段 | 是否必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 测试集格式版本 |
| `case_id` | 是 | 当前 case 的唯一 ID |
| `description` | 是 | case 的自然语言说明 |
| `chat_id` | 是 | 模拟飞书群聊 ID，也是 Chat Memory Space 边界 |
| `test_type` | 是 | 当前专项测试类型 |
| `replay_policy` | 是 | 回放策略说明 |
| `messages` | 是 | 完整历史对话流 |
| `final_query_messages` | 是 | 历史对话结束后的统一查询消息 |
| `expected` | 是 | 预期结果、预期答案和评分标准 |

---

## 2. `schema_version`

表示测试集格式版本。常见取值：

```json
"anti_noise_single_stream_v1"
"conflict_single_stream_v1"
"conflict_single_stream_v2"
"accuracy_single_stream_v1"
```

建议命名规则：

```text
测试类型 + single_stream + 版本号
```

---

## 3. `case_id`

当前测试 case 的唯一 ID。

示例：

```json
"case_id": "anti_noise_001_light"
```

建议命名规则：

```text
测试类型_序号_补充说明
```

例如：

```text
anti_noise_001_light
anti_noise_002_medium
anti_noise_003_heavy
conflict_002_supersede
conflict_003_false_positive
accuracy_001_memory_card
accuracy_002_evidence_block
accuracy_003_topic_summary
```

---

## 4. `test_type`

当前测试类型。

| test_type | 含义 |
|---|---|
| `anti_noise` | 抗干扰测试，考察系统在噪声和时间跨度后能否召回早期关键记忆 |
| `conflict_supersede` | 矛盾覆盖测试，考察新决策是否能覆盖旧决策 |
| `conflict_false_positive` | 误冲突测试，考察相似但不冲突的信息是否不会误覆盖旧决策 |
| `accuracy_memory_card` | 具体决策类准确率测试 |
| `accuracy_evidence_block` | 原话、来源、证据追溯类准确率测试 |
| `accuracy_topic_summary` | 整体方案、当前边界、高层摘要类准确率测试 |

---

## 5. `replay_policy`

`replay_policy` 用来说明 runner 应该如何回放这个 case。

示例：

```json
{
  "mode": "single_stream_with_final_queries",
  "description": "messages 是一段连续、完整的历史对话，不预先切分 batch；系统后续自行完成时间窗口切分或 Event Segmentation。final_query_messages 是历史对话结束后的统一查询入口。",
  "segmentation_policy": "测试集不预切 batch。被测系统需要基于 messages 的 create_time 和语义内容自行完成切分、沉淀与检索。",
  "scope_control": [
    "messages 中不包含 @机器人查询",
    "messages 中不包含日程/待办触发类信息",
    "final_query_messages 只用于最终召回测试"
  ]
}
```

### 5.1 `mode`

当前统一使用：

```json
"single_stream_with_final_queries"
```

含义：

```text
先将 messages 作为完整历史对话流送入系统；
系统完成记忆沉淀后；
再将 final_query_messages 作为最终查询统一发送。
```

### 5.2 `segmentation_policy`

说明当前 case 不预先切分 batch。

推荐写法：

```json
"segmentation_policy": "测试集不预切 batch。被测系统需要基于 messages 的 create_time 和语义内容自行完成切分、沉淀与检索。"
```

### 5.3 `scope_control`

说明当前专项测试中刻意排除的其他能力，避免维度混杂。

抗干扰测试中可以写：

```json
[
  "messages 中不包含 @机器人查询",
  "messages 中不包含日程/待办触发类信息",
  "messages 中不包含矛盾更新专项内容",
  "final_query_messages 只用于最终召回测试"
]
```

矛盾更新测试中可以写：

```json
[
  "messages 中不包含 @机器人查询",
  "messages 中不包含日程/待办触发类信息",
  "messages 中不包含抗干扰专项的大量无关噪声",
  "本 case 只评估新旧决策关系是否处理正确"
]
```

准确率测试中可以写：

```json
[
  "messages 中不包含 @机器人查询",
  "messages 中不包含需要创建日程或待办的即时动作",
  "messages 中不包含冲突覆盖专项内容",
  "最终查询统一放在 final_query_messages 中"
]
```

---

## 6. `messages`

`messages` 是完整历史对话流，模拟真实飞书群聊中一段连续发生的历史消息。

示例：

```json
{
  "content": "{\"text\": \"我建议 P1 先只做教材和考试资料，先别做全品类二手市场。\"}",
  "create_time": "2026-04-21 09:00",
  "deleted": false,
  "message_id": "om_anti_light_stream_0001",
  "msg_type": "text",
  "sender": {
    "id": "ou_001",
    "id_type": "open_id",
    "sender_type": "user",
    "tenant_key": "demo_tenant"
  },
  "updated": false
}
```

字段说明：

| 字段 | 是否必填 | 说明 |
|---|---|---|
| `content` | 是 | 飞书消息内容。文本消息使用 JSON 字符串：`{"text":"消息内容"}` |
| `create_time` | 是 | 消息创建时间，格式为 `YYYY-MM-DD HH:MM` |
| `deleted` | 是 | 是否删除，通常为 `false` |
| `message_id` | 是 | 消息唯一 ID，用于证据追溯 |
| `msg_type` | 是 | 消息类型，常见为 `text` / `interactive` |
| `sender` | 是 | 发送者信息 |
| `updated` | 是 | 是否更新，通常为 `false` |

### 6.1 `content`

文本消息格式：

```json
"content": "{\"text\": \"消息内容\"}"
```

注意：这里是字符串，不是对象。

卡片噪声格式示例：

```json
{
  "msg_type": "interactive",
  "content": "<card title=\"通知\">...</card>"
}
```

### 6.2 `create_time`

格式：

```text
YYYY-MM-DD HH:MM
```

用途：

```text
1. 模拟真实群聊时间流；
2. 测试系统的时间跨度抗干扰能力；
3. 供系统后续自行做时间窗口切分或 Event Segmentation。
```

### 6.3 `sender`

模拟飞书 sender 字段。

结构：

```json
{
  "id": "ou_001",
  "id_type": "open_id",
  "sender_type": "user",
  "tenant_key": "demo_tenant"
}
```

---

## 7. `final_query_messages`

`final_query_messages` 是历史对话结束后统一发送的测试问题。

示例：

```json
{
  "content": "{\"text\": \"@机器人 之前 P1 为什么只做教材和考试资料？\"}",
  "create_time": "2026-04-24 19:00",
  "deleted": false,
  "message_id": "om_anti_light_stream_1001",
  "msg_type": "text",
  "sender": {
    "id": "ou_002",
    "id_type": "open_id",
    "sender_type": "user",
    "tenant_key": "demo_tenant"
  },
  "updated": false
}
```

说明：

```text
messages 用于历史记忆沉淀；
final_query_messages 用于最终查询测试。
```

专项测试中，所有查询都应放在 `final_query_messages`，不要混在 `messages` 里。

---

## 8. `expected`

`expected` 是该 case 的预期结果，用于 evaluator 或 LLM-as-Judge 打分。

基本结构：

```json
{
  "target_memories": [],
  "relation_checks": [],
  "target_evidence_blocks": [],
  "target_topics": [],
  "noise_profile": {},
  "final_memory_checks": [],
  "llm_judge_rubric": {}
}
```

不同测试类型会使用不同字段。

---

## 9. `expected.target_memories`

用于描述系统应该沉淀出的关键 MemoryCard 或长期记忆。

示例：

```json
{
  "memory_id": "target_001",
  "description": "P1 品类边界：只做教材和考试资料，不做全品类二手市场。",
  "expected_granularity": "memory_card",
  "expected_keywords": ["教材", "考试资料", "不做全品类", "审核", "纠纷"],
  "source_message_ids": [
    "om_anti_light_stream_0001",
    "om_anti_light_stream_0002"
  ]
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `memory_id` | 测试集中自定义的预期记忆 ID |
| `description` | 预期记忆说明 |
| `expected_granularity` | 预期粒度，通常为 `memory_card` |
| `expected_keywords` | 该记忆应包含或能回答出的关键词 |
| `source_message_ids` | 该记忆应追溯到的原始消息 ID |
| `expected_status_after_update` | 矛盾更新测试中使用，常见为 `active` / `deprecated` |

---

## 10. `expected.relation_checks`

用于矛盾更新测试，描述新旧记忆之间的关系。

示例：

```json
{
  "relation_type": "supersedes",
  "old_expected_keywords": ["P1 不支持语音日记"],
  "new_expected_keywords": ["支持语音转文字输入"],
  "expected_explanation": "新决策改变了是否支持语音输入的结论，因此应覆盖旧决策。"
}
```

常见关系：

| relation_type | 含义 |
|---|---|
| `supersedes` | 新决策覆盖旧决策，旧版本应变为 deprecated |
| `refines` | 新决策补充或细化旧决策，但不推翻旧结论 |
| `related_to` | 新旧信息相关，但不存在覆盖或细化关系 |

误冲突测试中可以使用：

```json
{
  "relation_type": "related_to",
  "forbidden_relation_type": "supersedes"
}
```

含义：这两条记忆只应相关，不应被判断为覆盖关系。

---

## 11. `expected.target_evidence_blocks`

用于 EvidenceBlock 准确率测试，描述应被追溯到的证据块。

示例：

```json
{
  "block_id": "scope_boundary",
  "description": "文献综述辅助，不做论文代写。",
  "source_message_ids": [
    "om_accuracy_evidence_research_0001",
    "om_accuracy_evidence_research_0002"
  ]
}
```

---

## 12. `expected.target_topics`

用于 TopicSummary 准确率测试，描述应聚合出的高层主题。

示例：

```json
{
  "topic_id": "memory_architecture",
  "description": "EvidenceBlock / MemoryCard / TopicSummary 三层记忆",
  "expected_memory_count_min": 3
}
```

说明：`topic_summary` case 的历史对话不能太少，因为 TopicSummary 是多个 MemoryCard 聚合后的高层摘要。建议一个 topic 至少有 2-3 条 MemoryCard 支撑。

---

## 13. `expected.noise_profile`

用于抗干扰测试，描述噪声强度和时间跨度。

示例：

```json
{
  "history_message_count": 96,
  "time_span": "约 5 天",
  "noise_level": "medium"
}
```

---

## 14. `expected.final_memory_checks`

最重要的评测字段。每个元素对应一个最终查询问题及其预期答案。

示例：

```json
{
  "query_message_id": "om_anti_light_stream_1001",
  "query": "之前 P1 为什么只做教材和考试资料？",
  "expected_answer": "P1 只做教材和考试资料，是因为项目不想扩成全品类二手市场。全品类会引入电子产品、衣服、宿舍用品等复杂品类，审核、纠纷处理和交易治理都会变重；教材和考试资料的校园场景更明确、品类更可控，也更适合比赛 demo 展示。",
  "expected_granularity": "memory_card",
  "expected_keywords": ["教材", "考试资料", "不做全品类", "审核", "纠纷"],
  "forbidden_keywords": ["P1 做全品类", "支持衣服交易"]
}
```

字段说明：

| 字段 | 是否必填 | 说明 |
|---|---|---|
| `query_message_id` | 是 | 对应 `final_query_messages` 中的问题消息 ID |
| `query` | 是 | 最终测试问题的文本内容 |
| `expected_answer` | 是 | 预期答案，用于 LLM 语义打分 |
| `expected_granularity` | 是 | 预期返回粒度 |
| `expected_keywords` | 是 | 预期回答应覆盖的关键词 |
| `forbidden_keywords` | 是 | 回答中不应出现的错误结论关键词 |
| `expected_source_message_ids` | 否 | EvidenceBlock 测试中使用，表示期望返回的来源消息 ID |

### 14.1 `expected_granularity`

常见取值：

| expected_granularity | 查询类型 |
|---|---|
| `memory_card` | 问具体决策、理由、当前结论 |
| `evidence_block` | 问原话、谁说的、来源在哪 |
| `topic_summary` | 问整体方案、当前边界、已定事项总览 |

---

## 15. `expected.llm_judge_rubric`

用于 LLM-as-Judge 的评分标准。

示例：

```json
{
  "score_2": "回答语义完全匹配 expected_answer，包含核心决策、理由和当前状态。",
  "score_1": "回答大体正确，但缺少部分理由或来源。",
  "score_0": "回答错误、混入噪声决策、使用相反结论或编造。"
}
```

建议采用 0-2 分制：

| 分数 | 含义 |
|---|---|
| 2 | 完全正确 |
| 1 | 部分正确 |
| 0 | 错误 |

---

## 16. 推荐 evaluator 流程

建议 evaluator 分为两层：

```text
规则校验
  ↓
LLM 语义打分
```

规则校验包括：

```text
1. actual_answer 是否命中 expected_keywords；
2. actual_answer 是否包含 forbidden_keywords；
3. 返回粒度是否等于 expected_granularity；
4. 如果有 expected_source_message_ids，是否返回了正确来源。
```

LLM-as-Judge 输入建议包括：

```json
{
  "query": "...",
  "expected_answer": "...",
  "actual_answer": "...",
  "expected_granularity": "...",
  "actual_granularity": "...",
  "expected_keywords": [],
  "forbidden_keywords": [],
  "rubric": {}
}
```

LLM 输出建议为：

```json
{
  "score": 2,
  "is_semantically_correct": true,
  "granularity_correct": true,
  "has_forbidden_error": false,
  "missing_points": [],
  "explanation": "回答正确覆盖了 P1 决策和理由。"
}
```

---

## 17. 三类专项测试使用说明

### 17.1 抗干扰测试

目标：测试系统在时间跨度和大量噪声后，是否还能召回早期关键决策。

重点字段：

```json
"test_type": "anti_noise"
"expected.noise_profile"
"expected.final_memory_checks"
```

应避免：

```text
1. messages 中混入 @机器人查询；
2. messages 中混入日程/待办；
3. messages 中混入矛盾覆盖。
```

### 17.2 矛盾更新测试

目标：测试系统是否能正确识别 `supersedes` / `refines` / `related_to`。

重点字段：

```json
"test_type": "conflict_supersede"
"expected.target_memories"
"expected.relation_checks"
"expected.final_memory_checks"
```

应关注：

```text
1. 旧版本是否 deprecated；
2. 新版本是否 active；
3. 查询是否优先返回 active 新版本；
4. 看似相关但不冲突的信息是否不会误判为 supersedes。
```

### 17.3 准确率测试

目标：测试系统回答内容和返回粒度是否正确。

分为：

```text
accuracy_memory_card
accuracy_evidence_block
accuracy_topic_summary
```

其中：

```text
memory_card：具体决策问题
evidence_block：原话、谁说的、来源在哪
topic_summary：整体方案、当前边界、已定事项总览
```

---

## 18. 最小 case 示例

```json
{
  "schema_version": "accuracy_single_stream_v1",
  "case_id": "accuracy_demo_001",
  "description": "示例 case",
  "chat_id": "oc_demo",
  "test_type": "accuracy_memory_card",
  "replay_policy": {
    "mode": "single_stream_with_final_queries",
    "segmentation_policy": "测试集不预切 batch。"
  },
  "messages": [
    {
      "content": "{\"text\": \"P1 先只做个人日记，不做公开社区。\"}",
      "create_time": "2026-04-21 09:00",
      "deleted": false,
      "message_id": "om_demo_0001",
      "msg_type": "text",
      "sender": {
        "id": "ou_001",
        "id_type": "open_id",
        "sender_type": "user",
        "tenant_key": "demo_tenant"
      },
      "updated": false
    }
  ],
  "final_query_messages": [
    {
      "content": "{\"text\": \"@机器人 P1 做不做公开社区？\"}",
      "create_time": "2026-04-22 09:00",
      "deleted": false,
      "message_id": "om_demo_1001",
      "msg_type": "text",
      "sender": {
        "id": "ou_002",
        "id_type": "open_id",
        "sender_type": "user",
        "tenant_key": "demo_tenant"
      },
      "updated": false
    }
  ],
  "expected": {
    "final_memory_checks": [
      {
        "query_message_id": "om_demo_1001",
        "query": "P1 做不做公开社区？",
        "expected_answer": "P1 不做公开社区，只做个人日记。",
        "expected_granularity": "memory_card",
        "expected_keywords": ["不做公开社区", "个人日记"],
        "forbidden_keywords": ["做公开社区"]
      }
    ],
    "llm_judge_rubric": {
      "score_2": "完全正确",
      "score_1": "部分正确",
      "score_0": "错误"
    }
  }
}
```

---

## 19. 编写新 case 的建议

写新 case 时建议遵循：

```text
1. 一个 case 只测一个专项能力；
2. 不要在抗干扰 case 中混入冲突更新；
3. 不要在准确率 case 中混入日程/待办；
4. 所有最终问题都放在 final_query_messages；
5. expected_answer 要写成语义判断的标准答案，而不是关键词堆砌；
6. expected_keywords 用于规则辅助，forbidden_keywords 用于抓明显错误；
7. source_message_ids 要尽量指向真实消息，方便证据追溯。
```

推荐数量：

```text
抗干扰：3 个 case，轻/中/重
矛盾更新：2-3 个 case，supersedes / false_positive / refines
准确率：3 个 case，memory_card / evidence_block / topic_summary
```
