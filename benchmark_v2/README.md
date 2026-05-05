# benchmark_v2

这是当前仓库里推荐作为主 benchmark 使用的一套。

它与另外两套 benchmark 的关系不是替代，而是并列分工：

- `benchmark/full_demo_dual_channel_test.py`
  - 最小双通道回放基线
  - 用来测 replay 顺序和 adapter 兼容性

- `benchmark/special_case/special_case_replay.py`
  - 单流专项回放
  - 用来测 anti-noise / conflict / final query 等重型专项

- `benchmark_v2/`
  - 生产级 benchmark 主套件
  - 用来做 source/runtime 分层、轻量评测、深度评测、维度筛选和报告输出

如果问题是“当前 benchmark 能不能合并进主线并满足我们这边的任务”，答案是：

- 能合并；
- 但应作为主 benchmark 套件并行接入，而不是强行替换旧 `benchmark/`；
- 当前任务如果是“满足真实模拟效果、清晰分类测评标准、设计良好指标、形成有说服力的测试能力”，应以这套为主，旧 `full_demo` 和 `special_case` 为补充。

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
- 报告已补齐前两类生产级指标：
  - 性能 / 吞吐指标：`case_total_runtime_ms`、`avg/p95_realtime_latency_ms`、`avg/p95_write_latency_ms`、`realtime_throughput_msgs_per_sec`、`write_throughput_units_per_sec`
  - 召回排序指标：`queries`、`top1_hits`、`top3_hits`、`top1_hit_rate`、`top3_hit_rate`、`avg_retrieval_latency_ms`
- 报告补齐后两类专项指标：
  - 干扰对抗指标：`interference_metrics.batch_pass_rate`、`realtime_action_match_rate`、`write_count_match_rate`、`ignore_rule_match_rate`
  - 矛盾更新指标：`conflict_metrics.batch_pass_rate`、`memory_card_match_rate`、`relation_match_rate`、`forbidden_relation_match_rate`
- 报告继续补了质量诊断指标：
  - 写入质量：`write_quality_metrics.memory_card_match_rate`、`relation_match_rate`、`topic_match_rate`
  - 检索/证据质量：`retrieval_quality_metrics.final_memory_hit_rate`、`evidence_hit_rate`、`granularity_hit_rate`

当前口径说明：

- 性能 / 吞吐指标默认始终输出，适合看回放开销、写入开销、规则触发退化。
- 召回排序指标只在 case 级 deep eval 生效时输出；当前基于 `final_memory_checks` 构建，用来衡量终态记忆的 Top1 / Top3 命中，而不是替代真实线上检索评测。
- 干扰对抗 / 矛盾更新指标基于 v2 场景池中已标注的专项 batch 汇总，适合看分类、忽略规则、关系落地和误冲突回归。
- 写入质量指标用于判断 deep eval 失败主要卡在卡片、关系还是 topic。
- 检索/证据质量指标用于判断 final query、粒度命中和 evidence 追溯是否过关。
- `forbidden_relation_match_rate` 专门用来衡量误冲突保护，即“看起来相关，但不应被判成 supersedes”。

当前验证效果：

- `validate_fixture` 已通过，说明 source/runtime 结构一致性正常。
- `tests.test_benchmark_v2_runner`、`tests.test_benchmark_replay` 已通过，说明 runner、reporting、FULL_WRITE 语义兼容性正常。
- 轻量筛选回放已通过，说明按 `--tag` / `--chat` 的专项回归链路正常。
- deep 子集运行已能真实触发 `expected_memory_cards`、`expected_evidence_checks`、`forbidden_relation_type` 等校验。
- 当前 deep 失败主要暴露系统真实能力缺口，而不是 benchmark 本身故障：
  - realtime 分类基本稳定；
  - ignore 规则基本稳定；
  - evidence / card / relation 在部分场景仍未稳定命中。

统一入口：

```bash
conda run -n feishu-ai-p0 python -m benchmark.run_suite --suite v2
```
