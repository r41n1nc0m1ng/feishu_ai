# full_demo_case 字段结构说明

本文档用于说明 `full_demo_case` 测试集格式。该格式用于模拟飞书群聊中的双通道处理流程：

1. **实时层**：每个 batch 内的消息先按顺序逐条发送给实时层，用于测试 @机器人、群内提问、日程/待办等即时触发。
2. **写入层**：该 batch 全部实时处理完成后，再将整个 batch 发送给写入层，用于测试 Evidence Block、Memory Card、Topic Summary、版本关系等长期记忆沉淀能力。

该测试集的核心原则是：

- `messages` 中只放真实飞书接口可以获得的字段；
- `expected_*` 字段只作为 Benchmark 标注，不进入业务系统；
- 一个 batch 内可以包含多个决策；
- 一个 batch 也可能只包含半截话题，信息不足时不要求生成正式 Memory Card；
- 查询消息、日程/待办触发消息和噪声消息可以进入实时层，但写入层应忽略它们，避免污染长期记忆。

---

## 1. 顶层结构

```json
{
  "schema_version": "dual_channel_benchmark_v2",
  "case_id": "full_demo_ai_resume_expanded_001",
  "description": "测试集说明",
  "chat_id": "oc_demo_ai_resume",
  "replay_policy": {},
  "batches": [],
  "expected": {}
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | 测试集格式版本，方便后续升级字段。 |
| `case_id` | string | 当前测试用例 ID。 |
| `description` | string | 当前测试用例描述。 |
| `chat_id` | string | 模拟的飞书群 ID。同一个 case 默认属于同一个群。 |
| `replay_policy` | object | 说明该 case 的回放规则。 |
| `batches` | array | 按时间分批的群聊消息，每个 batch 模拟一次“十分钟拉取”。 |
| `expected` | object | 整个 case 跑完后的全局预期结果。 |

---

## 2. replay_policy

```json
{
  "mode": "dual_channel_batch_replay",
  "description": "每个 batch 内的消息先按顺序逐条发送给实时层；该 batch 全部实时处理完成后，再将整个 batch 发送给写入层。",
  "write_layer_should_ignore": [
    "@机器人查询消息",
    "普通历史提问",
    "日程/待办触发消息",
    "interactive 噪声卡片",
    "机器人或系统噪声"
  ],
  "partial_topic_policy": "如果 batch 末尾只有半截话题且信息不足，不要求生成正式 MemoryCard；如果半截话题已经包含明确决策信息，则允许按当前包含的信息生成 MemoryCard，后续 batch 再通过 refines/supersedes 补充。"
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `mode` | string | 回放模式。当前固定为 `dual_channel_batch_replay`。 |
| `description` | string | 对回放方式的自然语言说明。 |
| `write_layer_should_ignore` | array | 写入层不应沉淀为长期记忆的消息类型。 |
| `partial_topic_policy` | string | 跨 batch 半截话题的处理规则。 |

这部分主要给开发和评测人员阅读，不直接进入业务系统。

---

## 3. batches

`batches` 是测试集主体。每个 batch 模拟一次十分钟抓取窗口。

```json
{
  "batch_id": "batch_001",
  "fetch_time": "2026-04-29 10:10",
  "messages": [],
  "expected_realtime_results": [],
  "expected_write_result": {}
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `batch_id` | string | 批次 ID。 |
| `fetch_time` | string | 模拟写入层在这个时间点抓取该 batch 的消息。 |
| `messages` | array | 该批次内的飞书消息，格式贴近真实接口。 |
| `expected_realtime_results` | array | 这些消息逐条进入实时层时，预期系统行为。 |
| `expected_write_result` | object | 整个 batch 进入写入层后，预期生成什么记忆。 |

---

## 4. messages

`messages` 中每条消息尽量只使用真实飞书接口能拿到的字段。

```json
{
  "content": "{\"text\":\"我觉得这次不要做企业级记忆了，权限太复杂。\"}",
  "create_time": "2026-04-29 10:00",
  "deleted": false,
  "message_id": "om_resume_0001",
  "msg_type": "text",
  "sender": {
    "id": "ou_pm_001",
    "id_type": "open_id",
    "sender_type": "user",
    "tenant_key": "demo_tenant"
  },
  "updated": false
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `content` | string | 飞书消息内容。`text` 类型通常是 JSON 字符串，如 `{"text":"..."}`。 |
| `create_time` | string | 消息创建时间。 |
| `deleted` | boolean | 消息是否已删除。 |
| `message_id` | string | 飞书消息 ID。 |
| `msg_type` | string | 消息类型，如 `text`、`interactive`。 |
| `sender` | object | 发送者信息。 |
| `updated` | boolean | 消息是否被更新。 |

注意：

- 不要在 `messages` 中放 `sender_name`、`text`、`timestamp` 等真实接口不一定直接提供的字段。
- 如果需要解析文本，由测试 runner 或业务层从 `content` 中解析。
- `interactive` 类型消息可以作为噪声数据，用于测试系统是否会误记。

---

## 5. expected_realtime_results

这一层描述：**batch 内每条消息逐条发给实时层时，系统应该做什么。**

```json
{
  "message_id": "om_resume_0050",
  "expected_realtime_action": "retrieve_memory",
  "expected_granularity": "memory_card",
  "expected_keywords": [
    "不做完整 ATS",
    "JD",
    "简历匹配",
    "初筛"
  ]
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `message_id` | string | 对应 `messages` 中某条消息。 |
| `expected_realtime_action` | string | 实时层预期动作。 |
| `expected_granularity` | string | 如果触发检索，预期返回的记忆粒度。可选。 |
| `expected_keywords` | array | 返回内容中应包含的关键词。可选。 |
| `forbidden_keywords` | array | 返回内容中不应出现的关键词。可选。 |

`expected_realtime_action` 建议枚举：

| 值 | 含义 |
|---|---|
| `none` | 不触发任何即时动作。 |
| `retrieve_memory` | 触发历史记忆检索。 |
| `schedule_confirm` | 触发日程确认。 |
| `task_confirm` | 触发待办确认。 |
| `no_memory_found` | 触发检索，但当前还没有相关记忆。 |

示例：日程确认。

```json
{
  "message_id": "om_resume_0051",
  "expected_realtime_action": "schedule_confirm",
  "expected_keywords": [
    "Demo 评审会",
    "明天下午 3 点"
  ]
}
```

---

## 6. expected_write_result

这一层描述：**整个 batch 发送给写入层后，系统应该生成什么。**

```json
{
  "expected_evidence_blocks": "multiple_allowed",
  "expected_memory_cards": [],
  "optional_progress_cards": [],
  "expected_relations": [],
  "should_ignore_message_ids": []
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `expected_evidence_blocks` | number/string | 预期生成的 EvidenceBlock 数量。可以是具体数字，也可以是 `multiple_allowed`。 |
| `expected_memory_cards` | array | 该 batch 预期生成的正式 MemoryCard。 |
| `optional_progress_cards` | array | 半截话题或未决讨论，可以生成 Progress，也可以不生成。 |
| `expected_relations` | array | 该 batch 预期产生的记忆关系，如 `refines`、`supersedes`。 |
| `should_ignore_message_ids` | array | 写入层应忽略的消息，比如 @机器人查询、日程、噪声卡片。 |

---

## 7. expected_memory_cards

表示该 batch 应沉淀出的中粒度记忆。

```json
{
  "expected_granularity": "memory_card",
  "expected_status": "active",
  "expected_decision_object_keywords": [
    "MVP 范围",
    "完整 ATS"
  ],
  "expected_keywords": [
    "不做完整 ATS",
    "JD",
    "简历匹配分析",
    "初筛分析"
  ],
  "source_message_ids": [
    "om_resume_0001",
    "om_resume_0002",
    "om_resume_0003"
  ]
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `expected_granularity` | string | 预期粒度，通常是 `memory_card`。 |
| `expected_status` | string | 预期状态，如 `active`、`deprecated`、`progress`。 |
| `expected_decision_object_keywords` | array | `decision_object` 中应包含的关键词。 |
| `expected_keywords` | array | MemoryCard 的 `decision` / `reason` / `title` 中应包含的关键词。 |
| `forbidden_keywords` | array | 不应出现的关键词。可选。 |
| `source_message_ids` | array | 这条 MemoryCard 应追溯到的来源消息。 |

注意：

- `expected_keywords` 不要求逐字匹配，只要求语义结果中包含核心关键词。
- `source_message_ids` 用于检查 EvidenceBlock 来源追溯是否正确。

---

## 8. optional_progress_cards

用于处理“半截话题”或“未形成一致决策”的情况。

```json
{
  "expected_decision_object_keywords": [
    "匹配分"
  ],
  "expected_keywords": [
    "匹配分",
    "分数",
    "解释"
  ],
  "reason": "batch_002 中匹配分只开始讨论，PM 明确说先别定，因此不要求生成正式决策。"
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `expected_decision_object_keywords` | array | 如果生成 Progress，议题中应包含的关键词。 |
| `expected_keywords` | array | 如果生成 Progress，内容中应包含的关键词。 |
| `reason` | string | 为什么这条是可选 Progress，而不是必需正式决策。 |

含义：

- 如果系统生成 Progress 记忆，可以检查它是否包含这些关键词；
- 如果系统不生成，也不算失败。

适用场景：

1. batch 末尾话题说了一半；
2. 群里只是提出问题，没有形成共识；
3. 信息有价值，但不足以生成正式决策。

---

## 9. expected_relations

用于检查记忆关系，比如补充、覆盖、相关。

```json
{
  "relation_type": "refines",
  "old_expected_keywords": [
    "不输出自动淘汰",
    "辅助判断"
  ],
  "new_expected_keywords": [
    "匹配参考分",
    "推荐等级",
    "needs_review"
  ]
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `relation_type` | string | 关系类型，如 `related_to`、`refines`、`supersedes`、`contradicts`。 |
| `old_expected_keywords` | array | 旧记忆中应包含的关键词。 |
| `new_expected_keywords` | array | 新记忆中应包含的关键词。 |

关系含义：

| 关系 | 含义 |
|---|---|
| `related_to` | 相关，但不改变原记忆。 |
| `refines` | 补充或细化旧记忆，旧记忆仍可 active。 |
| `supersedes` | 覆盖旧记忆，旧记忆应 deprecated。 |
| `contradicts` | 存在冲突，但尚未完成覆盖。 |

---

## 10. should_ignore_message_ids

表示写入层不应生成长期记忆的消息。

```json
{
  "should_ignore_message_ids": [
    "om_resume_0050",
    "om_resume_0051",
    "om_resume_0060"
  ]
}
```

常见应忽略消息：

- @机器人查询消息；
- 普通历史提问；
- 日程/待办触发消息；
- interactive 噪声卡片；
- 系统通知；
- 闲聊。

注意：

- 这些消息可以进入实时层处理；
- 但不应该沉淀为 MemoryCard。

---

## 11. 顶层 expected

`expected` 是整个 case 跑完后的最终检查。

```json
{
  "final_memory_checks": [],
  "relation_checks": [],
  "action_checks": [],
  "evidence_checks": []
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `final_memory_checks` | array | 整个 case 结束后，用查询检查最终记忆是否正确。 |
| `relation_checks` | array | 检查最终是否形成正确记忆关系。 |
| `action_checks` | array | 检查日程/待办是否被正确触发。 |
| `evidence_checks` | array | 检查是否能追溯到正确 EvidenceBlock。 |

---

## 12. final_memory_checks

```json
{
  "query": "当前 AI 简历初筛 MVP 的整体边界是什么？",
  "expected_granularity": "topic_summary",
  "expected_keywords": [
    "不做完整 ATS",
    "PDF",
    "JD",
    "简历匹配",
    "匹配参考分",
    "人工复核",
    "mock 文件"
  ],
  "forbidden_keywords": []
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `query` | string | 最终测试查询。 |
| `expected_granularity` | string | 预期回答粒度。 |
| `expected_keywords` | array | 回答中应包含的关键词。 |
| `forbidden_keywords` | array | 回答中不应出现的关键词。 |

---

## 13. action_checks

```json
{
  "trigger_message_id": "om_resume_0051",
  "expected_action_type": "schedule",
  "expected_keywords": [
    "Demo 评审会",
    "明天下午 3 点"
  ]
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `trigger_message_id` | string | 触发日程/待办的消息 ID。 |
| `expected_action_type` | string | `schedule` 或 `task`。 |
| `expected_keywords` | array | 日程/待办卡片中应包含的关键词。 |

---

## 14. evidence_checks

```json
{
  "query": "当时是谁说不要自动淘汰候选人的？",
  "expected_granularity": "evidence_block",
  "expected_source_message_ids": [
    "om_resume_0008",
    "om_resume_0009",
    "om_resume_0010"
  ],
  "expected_keywords": [
    "AI 不能直接决定候选人命运",
    "不输出自动淘汰结论"
  ]
}
```

字段含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `query` | string | 来源追溯查询。 |
| `expected_granularity` | string | 预期返回 EvidenceBlock。 |
| `expected_source_message_ids` | array | 应命中的来源消息 ID。 |
| `expected_keywords` | array | 来源消息或回答中应包含的关键词。 |

---

## 15. 最小示例

下面是一个简化版，只保留一个 batch、两条消息、一条预期决策：

```json
{
  "schema_version": "dual_channel_benchmark_v2",
  "case_id": "mini_ai_resume_001",
  "description": "最小测试：AI 简历初筛项目范围决策",
  "chat_id": "oc_demo_ai_resume",
  "replay_policy": {
    "mode": "dual_channel_batch_replay",
    "description": "每个 batch 内消息先逐条进入实时层，再整包进入写入层。",
    "write_layer_should_ignore": [
      "@机器人查询消息",
      "日程/待办触发消息",
      "interactive 噪声卡片"
    ],
    "partial_topic_policy": "半截话题信息不足时不要求生成正式 MemoryCard。"
  },
  "batches": [
    {
      "batch_id": "batch_001",
      "fetch_time": "2026-04-29 10:10",
      "messages": [
        {
          "content": "{\"text\":\"AI 自动筛简历这个项目先不要做完整 ATS，范围太大。\"}",
          "create_time": "2026-04-29 10:00",
          "deleted": false,
          "message_id": "om_demo_001",
          "msg_type": "text",
          "sender": {
            "id": "ou_pm_001",
            "id_type": "open_id",
            "sender_type": "user",
            "tenant_key": "demo_tenant"
          },
          "updated": false
        },
        {
          "content": "{\"text\":\"同意，MVP 先做 JD 和简历匹配分析，不做候选人流转。\"}",
          "create_time": "2026-04-29 10:01",
          "deleted": false,
          "message_id": "om_demo_002",
          "msg_type": "text",
          "sender": {
            "id": "ou_dev_001",
            "id_type": "open_id",
            "sender_type": "user",
            "tenant_key": "demo_tenant"
          },
          "updated": false
        }
      ],
      "expected_realtime_results": [
        {
          "message_id": "om_demo_001",
          "expected_realtime_action": "none"
        },
        {
          "message_id": "om_demo_002",
          "expected_realtime_action": "none"
        }
      ],
      "expected_write_result": {
        "expected_evidence_blocks": 1,
        "expected_memory_cards": [
          {
            "expected_granularity": "memory_card",
            "expected_status": "active",
            "expected_decision_object_keywords": [
              "MVP 范围",
              "完整 ATS"
            ],
            "expected_keywords": [
              "不做完整 ATS",
              "JD",
              "简历匹配分析",
              "不做候选人流转"
            ],
            "source_message_ids": [
              "om_demo_001",
              "om_demo_002"
            ]
          }
        ],
        "should_ignore_message_ids": []
      }
    }
  ],
  "expected": {
    "final_memory_checks": [
      {
        "query": "我们为什么不做完整 ATS？",
        "expected_granularity": "memory_card",
        "expected_keywords": [
          "不做完整 ATS",
          "JD",
          "简历匹配分析"
        ],
        "forbidden_keywords": []
      }
    ],
    "relation_checks": [],
    "action_checks": [],
    "evidence_checks": []
  }
}
```

---

## 16. 一句话总结

这个测试集的核心结构是：

```text
batches 负责模拟“双通道回放”；
messages 只放真实飞书消息字段；
expected_realtime_results 检查实时层；
expected_write_result 检查写入层；
顶层 expected 检查整个 case 跑完后的最终记忆、关系、动作和证据追溯。
```
