# benchmark_v2

独立于 `benchmark/` 的离线双通道回放器 + 分层评测框架。

目标：
- 不改吴同学现有 benchmark；
- 单独承接专项样例；
- 后续可接真实写入链路与真实实时链路；
- 默认用轻量 expected 验证回放顺序和动作分类；
- 可选开启深度评测，检查 `expected_memory_cards` / `expected_relations` / `final_memory_checks`。

当前判定：
- 轻量模式：`realtime_actions`、`write_result_count`、`should_ignore_message_ids`
- 深度模式：MemoryCard / relation / TopicSummary / case 终态检查
- 深度模式通过 `BENCHMARK_V2_DEEP_EVAL=1` 显式开启，避免默认绑定本地模型状态。
- 支持按 `--tag` / `--chat` 做专项回归。

当前工作流：
- `scenario_source_v2.json` 是场景源文件，允许保留冰山式隐藏背景，仅用于构造合理事件、消息摘录、触发时序和标准答案。
- `full_demo_case_v2.json` 是最终运行 fixture，只保留 benchmark 运行所需字段。
- 通过下面的构建脚本从 source 生成最终 fixture：

```bash
python benchmark_v2/build_fixture_from_source.py
```

- 通过下面的校验脚本做 source/runtime 一致性检查：

```bash
python benchmark_v2/validate_fixture.py
```

当前 fixture 特点：
- 已从单一 demo 样例升级为专项覆盖包；
- 每个 batch 都有 `scenario` / `tags`，方便后续筛选执行；
- 已显式覆盖噪声过滤、action 消息、progress、refine、supersede、source/version/summary/topic 查询、多主题并行与稀疏窗口。
- 冰山式隐藏背景只保留在 source 文件，不进入最终 runtime fixture。
- 已引入非程序员主导场景，例如产品发布协调、客服口径统一、会议组织与会务分工。
- 已引入多群边界、客服规则、答辩准备、跨群漂移防护等现实协作场景。

附录：
- [APPENDIX_SOURCE_SCHEMA.md](/Users/davidai/Desktop/feishuai/feishu_ai/benchmark_v2/APPENDIX_SOURCE_SCHEMA.md)
- [APPENDIX_ICEBERG_METHOD.md](/Users/davidai/Desktop/feishuai/feishu_ai/benchmark_v2/APPENDIX_ICEBERG_METHOD.md)
- [PRODUCTION_BENCHMARK_SPEC.md](/Users/davidai/Desktop/feishuai/feishu_ai/benchmark_v2/PRODUCTION_BENCHMARK_SPEC.md)

仍未达到生产级的部分：
- 还没有几百条以上样本规模；
- 还没有分 tenant / 多 chat / 多语言 / 极端脏数据集；
- 还没有线上回归看板和长期稳定性趋势统计。

运行：

```bash
conda run -n feishu-ai-p0 python -m benchmark_v2.dual_channel_runner benchmark_v2/full_demo_case_v2.json
```

按标签或群筛选：

```bash
conda run -n feishu-ai-p0 python -m benchmark_v2.dual_channel_runner benchmark_v2/full_demo_case_v2.json --tag supersede --chat oc_proj_core
```

深度评测：

```bash
BENCHMARK_V2_DEEP_EVAL=1 FULL_WRITE=1 \
conda run -n feishu-ai-p0 python -m benchmark_v2.dual_channel_runner benchmark_v2/full_demo_case_v2.json
```

报告输出：

- 运行后会写入 `benchmark_v2/reports/benchmark_v2_latest.json`
- 报告包含 `by_chat`、`by_tag`、`realtime_action_distribution`
