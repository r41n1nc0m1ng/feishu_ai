# Special Case Replay 使用说明

`benchmark/special_case_replay.py` 用来回放 `benchmark/special_case/*.json` 专项测试集。它会先把历史消息按时间切成 batch，送入完整写入侧链路，再把 `final_query_messages` 发给当前查询侧，并显示实际回答和预期回答。

## 快速开始

查看可用参数：

```powershell
python -m benchmark.special_case.special_case_replay --help
```

跑单个 case：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json
```

跑整个目录：

```powershell
python -m benchmark.special_case.special_case_replay --dir benchmark/special_case
```

开启大模型评分：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/conflict_001_supersede.json --llm-judge
```

Graphiti 暂时不可用时，用 SQLite 卡片召回跑通测试：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --retriever-backend sqlite-keyword
```

## 自选参数怎么调

### 选择测试范围

只测一个文件时用 `--case`：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/conflict_001_supersede.json
```

批量跑目录时用 `--dir`：

```powershell
python -m benchmark.special_case.special_case_replay --dir benchmark/special_case
```

`--case` 和 `--dir` 二选一。不传时默认跑 `benchmark/special_case` 目录。

### 调整 batch 时间窗口

默认每 1 小时划一个 batch：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --batch-hours 1
```

如果想更细，可以改成 0.5 小时：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --batch-hours 0.5
```

如果想更粗，可以改成 2 小时：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --batch-hours 2
```

建议：

- 想模拟更频繁的写入侧拉取，用更小的 `--batch-hours`。
- 想模拟更长时间才同步一次消息，用更大的 `--batch-hours`。
- `--batch-hours` 必须大于 0。

### 并发处理 EvidenceBlock

默认按顺序处理每个 EvidenceBlock：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --llm-concurrency 1
```

如果想让测试脚本在一个 batch 内同时处理多个 EvidenceBlock，可以调大 `--llm-concurrency`：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --retriever-backend sqlite-keyword --llm-concurrency 3
```

这个参数只影响专项测试脚本，不改主链路。默认值是 1，所以不传参数时仍保持原来的顺序写入。

注意：并发会让多个 CardGenerator 同时读写内存缓存和 SQLite。它适合 anti-noise、流程调试、临时加速；如果 case 强依赖前后版本顺序、SUPERSEDE 或 ConflictDetector 判断，建议先用默认 `--llm-concurrency 1` 做最终验收。

### 并发处理 batch

默认按时间顺序逐个处理 batch：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --batch-concurrency 1
```

如果想让不同时间窗口的 batch 同时处理，可以调大 `--batch-concurrency`：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --retriever-backend sqlite-keyword --batch-concurrency 3
```

也可以和 EvidenceBlock 并发一起用：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --retriever-backend sqlite-keyword --batch-concurrency 3 --llm-concurrency 3
```

注意：batch 并发会打破“先处理早期消息，再处理后续消息”的严格顺序。它适合 anti-noise、吞吐调试、临时跑通；如果 case 测的是版本覆盖、冲突更新、追问依据、跨 batch 上下文延续，最终验收建议用默认 `--batch-concurrency 1`。

### 调整 overlap 消息数

默认每个非首批会额外带上上一非空时间窗口最后 3 条消息：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --overlap 3
```

不带历史重叠消息：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --overlap 0
```

增加上下文重叠，比如带上 5 条：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --overlap 5
```

注意：overlap 消息会直接混入下一个 batch，不额外标注、不去重、不作为特殊上下文处理。这是为了检验系统面对重复消息和跨窗口上下文时的真实表现。

### 是否清空已有数据

默认会清空测试数据后再运行：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json
```

清空范围包括：

- SQLite 当前项目数据库 `memory_store.db`
- Neo4j/Graphiti 中的数据
- 进程内缓存

如果想保留现有数据，在已有记忆基础上追加测试，用 `--no-reset`：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --no-reset
```

报告里会标记这是 dirty run。一般对比专项 case 时建议使用默认清空；调试累积记忆影响时再用 `--no-reset`。

### 选择查询召回后端

默认使用当前主链路的 Graphiti 召回：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --retriever-backend graphiti
```

如果 Graphiti 暂时跑不通，可以切到脚本内置的 SQLite 关键词召回：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --retriever-backend sqlite-keyword
```

这个模式会：

- 不初始化 Graphiti。
- 默认只清空 SQLite 和进程内缓存，不清 Neo4j。
- 写入侧仍走 `segment_async -> EvidenceStore.save -> CardGenerator.generate -> TopicManager.rebuild_topics`。
- 查询侧从 SQLite 的 `memory_cards` 读取 active 卡片，用标题、议题、决策、理由做字符重叠排序。

注意：`sqlite-keyword` 是专项测试脚本里的临时 fallback，用来在 Graphiti 不稳定时跑通回放和观察卡片效果。它不是正式的 SQLite embedding 召回，也不代表线上主链路已经切换。

### 是否开启 LLM judge

默认不开启大模型评分：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json
```

开启评分：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --llm-judge
```

LLM judge 只会收到：

- query
- expected_answer
- actual_reply

它不会收到 case 里的 `expected_keywords`、`forbidden_keywords` 或 rubric。模型必须只返回 `0`、`1` 或 `2`。如果返回格式不对，脚本会记录 `judge_error`，但不会中断整套测试。

### 指定报告路径

不传 `--report` 时，报告默认写到：

```text
benchmark/reports/
```

指定一个输出文件：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --report benchmark/reports/anti_noise_debug.json
```

跑目录时也可以指定一个汇总报告：

```powershell
python -m benchmark.special_case.special_case_replay --dir benchmark/special_case --report benchmark/reports/special_case_all.json
```

## 常用组合

默认验收单个 case：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json
```

看 batch overlap 是否影响结果：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --overlap 0
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/anti_noise_001_light.json --overlap 5
```

比较不同同步频率：

```powershell
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/conflict_001_supersede.json --batch-hours 0.5
python -m benchmark.special_case.special_case_replay --case benchmark/special_case/conflict_001_supersede.json --batch-hours 2
```

完整批量跑并开启评分：

```powershell
python -m benchmark.special_case.special_case_replay --dir benchmark/special_case --batch-hours 1 --overlap 3 --llm-judge
```

Graphiti 不可用时批量跑：

```powershell
python -m benchmark.special_case.special_case_replay --dir benchmark/special_case --batch-hours 1 --overlap 3 --retriever-backend sqlite-keyword
```

Graphiti 不可用且想临时加速：

```powershell
python -m benchmark.special_case.special_case_replay --dir benchmark/special_case --batch-hours 1 --overlap 3 --retriever-backend sqlite-keyword --batch-concurrency 3 --llm-concurrency 3
```

## 输出怎么看

终端会逐条显示：

- case id
- query id
- query text
- actual reply
- expected answer
- keyword check
- LLM score，如果开启了 `--llm-judge`

JSON 报告会保存同样信息，并额外包含 SQLite 诊断信息，例如卡片数、证据块数、topic 数、关系检查结果。

SQLite 只用于诊断当前状态，实际效果以查询侧返回的 `actual_reply` 为准。

## 依赖提醒

脚本会初始化 Graphiti/Neo4j。当前查询侧依赖 Graphiti 语义召回，所以如果 Neo4j 或 Graphiti 初始化失败，脚本会中止。

如果只是验证脚本内部逻辑，不想跑完整业务链路，可以运行单元测试：

```powershell
python -m unittest tests.test_special_case_replay
```
