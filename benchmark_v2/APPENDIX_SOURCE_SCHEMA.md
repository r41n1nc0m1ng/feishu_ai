# Scenario Source Schema

本文档定义 `scenario_source_v2.json` 的场景源 schema。它是 benchmark 构造层，不直接给 runner 使用。

## 目标

- 先构造真实任务情形
- 再摘出群内会出现的消息
- 再生成最终 `full_demo_case_v2.json`

## 顶层字段

与运行态 fixture 基本一致，但允许额外包含：

- `iceberg_policy`
- `batches[].iceberg_context`

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

## 约束

- `messages` 仍必须保持飞书接口可得字段风格
- `expected_*` 仍只写评测标注，不进入业务链路
- `iceberg_context` 只存在于 source，不进入 runtime fixture

