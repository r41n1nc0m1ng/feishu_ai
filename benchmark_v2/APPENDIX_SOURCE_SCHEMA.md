# Scenario Source Schema

本文档定义 `scenario_source_v2.json` 的场景源 schema。它是 benchmark 构造层，不直接给 runner 使用。

## 目标

- 先构造真实任务情形
- 再摘出群内会出现的消息
- 再生成最终 `full_demo_case_v2.json`

## 顶层字段

与运行态 fixture 基本一致，但允许额外包含：

- `iceberg_policy`
- `construction_method`
- `batches[].iceberg_context`
- `batches[].construction_context`

## 推荐构造流水线

推荐按下面顺序构造 source，而不是直接写最终消息：

1. `ontology_base`
   - 先定义群类型、角色、本轮任务对象、允许的消息类型、允许沉淀的记忆类型。
2. `timeline_base`
   - 先定绝对时间锚点、批次窗口、相对间隔、需要 lookback 的位置。
3. `thread_slots`
   - 给每个 batch 先分线程槽位，例如发布口径 / 会务动作 / 风险预案，避免一句话既像 query 又像 schedule 但没有上下文。
4. `iceberg_context`
   - 再补群外事件、隐含任务、角色顾虑、不可见前提。
5. `visible_projection`
   - 最后只投影群里真实会说的话到 `messages`。
6. `expectation_annotation`
   - 补 `expected_brief`、`expected`、`expected_write_result`、case 级 checks。

## `iceberg_context`

建议字段：

| 字段 | 说明 |
|---|---|
| `project_name` | 项目名 |
| `tenant_type` | 团队/组织类型 |
| `delivery_window` | 时间窗口 |
| `primary_group_goal` | 该群主目标 |
| `scene` | 这一批消息所处场景 |
| `work_item` | 当前具体工作项 |
| `deliverables` | 希望达成的产物 |
| `participants` | 参与角色与关注点 |
| `hidden_tasks` | 群外或隐含任务 |
| `off_group_events` | 群外已发生但会影响群内发言的事件 |
| `message_projection_rule` | 从真实任务到群消息的摘取规则 |
| `hidden_state.topic_stack` | 并行主题栈 |
| `hidden_state.emotional_tone` | 情绪/协作状态 |
| `hidden_state.non_visible_assumptions` | 群里没明说但实际影响决策的前提 |

## `construction_method`

顶层 source-only 元数据，用来说明整套 benchmark 的构造法。建议包含：

| 字段 | 说明 |
|---|---|
| `pipeline` | 构造步骤，例如 ontology -> timeline -> thread_slots -> iceberg -> projection |
| `ontology_axes` | 群、角色、消息行为、记忆对象等本体轴 |
| `why` | 为什么不直接生成最终消息 |

## `construction_context`

batch 级 source-only 元数据，建议至少覆盖下面两类信息：

| 字段 | 说明 |
|---|---|
| `timeline_base.anchor_time` | 本批时间锚点 |
| `timeline_base.window_minutes` | 本批窗口长度 |
| `timeline_base.relative_position` | 在主线中的相对位置，例如 seed / mid / long_gap_recall |
| `timeline_base.depends_on_batches` | 该批需要依赖哪些前序批次 |
| `thread_slots[].slot` | 线程槽位代号 |
| `thread_slots[].topic` | 该槽位承载的话题 |
| `thread_slots[].message_ids` | 槽位对应的可见消息 |
| `stress_target` | 这一批主要想打哪类误判 |

## 约束

- `messages` 仍必须保持飞书接口可得字段风格
- `expected_*` 仍只写评测标注，不进入业务链路
- `iceberg_context` / `construction_context` 只存在于 source，不进入 runtime fixture
