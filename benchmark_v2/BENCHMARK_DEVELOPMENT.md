# Benchmark V2 开发说明

本文档面向后续的专家 agent，目标是让它在不反复翻代码的前提下，直接理解 `benchmark_v2` 的数据结构、运行方式、系统交互、环境依赖和当前限制。

## 1. 目标

`benchmark_v2` 是一套独立于旧 `benchmark/` 的离线双通道回放体系，用来验证：

- 写入侧批处理是否能稳定消化历史消息；
- 实时侧是否能正确分类 `query / task / schedule / noop`；
- 记忆检索、TopicSummary、版本链、来源展开等能力是否可用；
- 多群聊、多主题、长线更新、对抗干扰等场景是否符合真实群聊逻辑。

它的定位不是演示版 fixture，而是可继续扩展的生产级 benchmark 框架。

## 2. 目录与文件

当前核心文件：

- `benchmark_v2/scenario_source_v2.json`
- `benchmark_v2/full_demo_case_v2.json`
- `benchmark_v2/build_fixture_from_source.py`
- `benchmark_v2/validate_fixture.py`
- `benchmark_v2/dual_channel_runner.py`
- `benchmark_v2/APPENDIX_SOURCE_SCHEMA.md`
- `benchmark_v2/APPENDIX_ICEBERG_METHOD.md`
- `benchmark_v2/PRODUCTION_BENCHMARK_SPEC.md`

建议优先阅读顺序：

1. `BENCHMARK_DEVELOPMENT.md`
2. `PRODUCTION_BENCHMARK_SPEC.md`
3. `APPENDIX_SOURCE_SCHEMA.md`
4. `dual_channel_runner.py`
5. `build_fixture_from_source.py`
6. `validate_fixture.py`

## 3. 双层数据结构

### 3.1 Source 层

文件：`scenario_source_v2.json`

用途：

- 用于构造真实任务情形；
- 允许保留 iceberg 式隐藏背景；
- 允许记录群外事件、隐含任务、角色压力、标准答案；
- 只用于 benchmark 构造，不直接给 runner 消费。

Source 层允许多出的字段：

- `iceberg_policy`
- `batches[].iceberg_context`

### 3.2 Runtime 层

文件：`full_demo_case_v2.json`

用途：

- 直接给 runner 运行；
- 只保留运行所需字段；
- 不要求强行兼容旧的 demo 格式；
- 重点是“可运行、可校验、可扩展”。

Runtime 层不应包含 `iceberg_context`。

## 4. 当前 runtime schema

### 4.1 顶层字段

当前 runtime 顶层通常包含：

- `schema_version`
- `case_id`
- `description`
- `chat_id`
- `replay_policy`
- `coverage_targets`
- `answer_policy`
- `evaluation_axes`
- `batches`
- `chat_profiles`

### 4.2 batch 字段

当前 batch 里通常包含：

- `batch_id`
- `scenario`
- `tags`
- `fetch_time`
- `expected_brief`
- `messages`
- `expected`
- `expected_write_result`
- `chat_id`
- `expected_realtime_results`

### 4.3 message 字段

每条消息至少应具备：

- `message_id`
- `msg_type`
- `create_time`
- `sender`
- `content`

实际构造时也可兼容：

- `timestamp`
- `text`
- `body.content`

但最终建议统一成可直接投喂项目入口的字段风格。

### 4.4 expected 字段

当前 runner 真正检查的还是轻量项：

- `expected.realtime_actions`
- `expected.write_result_count`
- `expected_write_result.should_ignore_message_ids`

在设置 `BENCHMARK_V2_DEEP_EVAL=1` 时，还会继续检查：

- `expected_memory_cards`
- `expected_relations`
- `expected_topic_summaries`
- 顶层 `expected.final_memory_checks`
- 顶层 `expected.relation_checks`

所以它现在已经具备基础评测骨架，但默认仍以离线稳定和环境可跑为先。
同时支持按 `--tag` / `--chat` 做专项回归，适合生产级分层测试。

## 5. 交互链路

### 5.1 写入侧

写入侧主路径：

`BatchProcessor -> fetch_messages -> event_segmenter -> EvidenceStore -> CardGenerator -> TopicManager`

关键行为：

- 周期轮询默认 60s；
- 群聊入群后可立即触发一次历史批处理；
- 如果新增消息出现在已有窗口中，会补前面若干条上下文；
- 默认对机器人自身消息和 `@机器人` 消息做跳过；
- 生成 `MemoryCard` 后可能触发 `TopicSummary` 重建。

### 5.2 实时侧

实时侧主路径：

`dispatch_message -> classify_realtime_action -> query_handler / action_handler`

当前分类逻辑：

- 显式 `@机器人` 或显式 mention 触发 query；
- task-like 触发 task；
- schedule-like 触发 schedule；
- 其余为 noop。

OpenClaw 仅作为可选回退：

- 仅当 `REALTIME_OPENCLAW_FALLBACK=1` 时才启用；
- 默认不会自动接管实时侧；
- 当前 bridge 先注入最近消息窗口和 memory hints，再调用 OpenClaw/Ollama。

### 5.3 记忆检索

检索侧当前约定：

- `MemoryRetriever.retrieve()` 仍保留 Graphiti 语义召回入口；
- `retrieve_topic_summary()` 直接读 SQLite 的 TopicSummary；
- `expand_evidence()` 直接展开 `EvidenceBlock`；
- 版本链通过 `supersedes_memory_id` 向上追溯。

## 6. Source 设计原则

Source 层允许使用 iceberg 方法，但 iceberg 只是构造工具，不是 runtime 结构。

建议遵守：

- 每个群聊有自己的主题边界；
- 不要把所有主题硬塞进同一个群；
- 长线多任务可以并行，但仍要围绕同一群主题展开；
- 非工程场景必须覆盖，例如产品发布、客服口径、会议组织、会务分工等；
- 群外事件可以影响群内发言，但最终进入 runtime 的仍是群内可见消息。

## 7. 环境依赖

### 7.1 Python/Conda

建议使用：

- conda 环境名：`feishu-ai-p0`
- 运行方式：`conda run -n feishu-ai-p0 ...`

### 7.2 LLM / Embedding

本地默认：

- `MODEL_PROVIDER=ollama`
- `LOCAL_MODEL=qwen2.5:7b`
- `EMBED_MODEL=nomic-embed-text`

默认向量模型和生成模型需要在本地 Ollama 可用。

### 7.3 Neo4j / Graphiti

如果要走 Graphiti 相关路径，需要：

- `NEO4J_URI`
- `NEO4J_USER`
- `NEO4J_PASSWORD`

但当前 P0/P1 的默认链路里，Graphiti 索引不应作为稳定主状态来源。

### 7.4 Feishu

需要正确配置：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_BOT_OPEN_ID`

如果要测日历/待办等扩展能力，还需要额外权限和对应能力开通。

### 7.5 关键环境变量

常用变量：

- `BATCH_POLL_INTERVAL=60`
- `INDEX_EVIDENCE_IN_GRAPHITI=false`
- `SEGMENTER_STRATEGY=time`
- `BLOCK_GAP_SECONDS=300`
- `MAX_BLOCK_MESSAGES=30`
- `LOG_LEVEL=INFO`
- `LOG_DIR=logs`
- `REALTIME_OPENCLAW_FALLBACK=0`

可选语义切分：

- `SEGMENTER_STRATEGY=semantic`
- `SEMANTIC_THRESHOLD`
- `MIN_BLOCK_MESSAGES`

## 8. 运行方式

### 8.1 从 source 构建 runtime fixture

```bash
python benchmark_v2/build_fixture_from_source.py
```

### 8.2 校验 source/runtime

```bash
python benchmark_v2/validate_fixture.py
```

### 8.3 运行离线回放

```bash
conda run -n feishu-ai-p0 python -m benchmark_v2.dual_channel_runner benchmark_v2/full_demo_case_v2.json
```

可筛选执行：

```bash
conda run -n feishu-ai-p0 python -m benchmark_v2.dual_channel_runner benchmark_v2/full_demo_case_v2.json --tag query --chat oc_proj_core
```

### 8.4 开启深度写入与深度评测

```bash
BENCHMARK_V2_DEEP_EVAL=1 FULL_WRITE=1 \
conda run -n feishu-ai-p0 python -m benchmark_v2.dual_channel_runner benchmark_v2/full_demo_case_v2.json
```

补充说明：

- `FULL_WRITE=1` 时，adapter 会直接走本地真实写入流水线：`segment_async -> EvidenceStore -> CardGenerator -> TopicManager`
- runner 会在运行前清理 benchmark 涉及 chat_id 的 SQLite 状态，避免重复执行污染结果
- 运行后会写出 `benchmark_v2/reports/benchmark_v2_latest.json`

### 8.5 运行整套测试

项目级测试：

```bash
conda run -n feishu-ai-p0 python -m unittest discover tests
```

## 9. 当前 runner 的验收边界

当前 `dual_channel_runner.py` 已做：

- 实时侧动作序列检查
- 写入结果数量检查
- 写入忽略消息检查
- 可选 MemoryCard / relation / TopicSummary 检查
- 顶层 case 终态检查

当前还没有真正完成：

- precision / recall / stability 的趋势统计；
- 更细粒度的语义相似度打分；
- 多语言 / 极脏数据 / 多租户专项；
- 线上回归历史看板。

但已经有：

- 现实逻辑 lint
- 分群 / 分 tag 执行
- 分维度 JSON 报表

这意味着：

- benchmark 已经能跑；
- 但还没到完整生产级评测器。

## 10. 当前已知注意事项

- 默认写入侧轮询间隔是 60 秒，适合调试，不适合最终线上节奏；
- `INDEX_EVIDENCE_IN_GRAPHITI=false` 时，EvidenceBlock 仅本地保存，Graphiti 边抽取不作为主状态来源；
- `event_segmenter` 默认是时间切分，语义切分是可选模式；
- `realtime` 当前是规则优先，OpenClaw 不是默认必经路径；
- 目前 benchmark 更看重“场景真实性”和“可复现运行”，不是 LLM 自动打分的幻觉式漂亮结果。

## 11. 专家 agent 建议任务

后续如果继续给专家 agent 开发，优先做这些：

1. 补全 `expected_write_result` 的校验；
2. 给 `TopicSummary`、`MemoryCard`、`EvidenceBlock` 增加更完整的验收路径；
3. 让对抗干扰、矛盾更新、长线召回有独立专项；
4. 扩充多群聊、多主题、多角色的真实样本；
5. 补统一的评测输出报表；
6. 给报告层补维度聚合和趋势对比；
7. 如果要做语义切分，再明确它与现有时间切分的边界。

## 12. 一句话原则

这个 benchmark 的原则是：

**source 负责造真场景，runtime 负责稳定跑，runner 负责轻验收，复杂语义评测后补。**
