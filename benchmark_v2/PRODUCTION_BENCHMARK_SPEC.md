# Production Benchmark Spec

## 目标

这套 benchmark 的目标不是演示，而是形成可持续扩展、可生成、可评测的生产级规范。

## 文件分层

### 1. 场景源层

文件：`scenario_source_v2.json`

职责：
- 记录真实任务背景
- 记录角色、交付物、群外事件
- 记录冰山式隐藏上下文
- 记录标准答案与结构化预期

这一层服务于“造 benchmark”，不直接给 runner 使用。

### 2. 运行层

文件：`full_demo_case_v2.json`

职责：
- 只保留 runner 需要的字段
- 以“可运行、可校验、可扩展”为准则，不强行兼容旧字段
- 允许增加 `expected_brief` 这种评测辅助字段

### 3. 评测层

文件：
- `dual_channel_runner.py`
- `evaluator.py`

职责：
- 校验 realtime action 序列
- 校验 write result 数量与 ignore 规则
- 可选校验 `expected_memory_cards` / `expected_relations` / `expected_topic_summaries`
- 校验 case 终态 `final_memory_checks` / `relation_checks`

说明：
- 默认轻量模式优先，保证环境适配和可重复执行
- 深度评测需显式开启，不默认依赖本地模型质量

### 4. 报表层

文件：
- `reporting.py`
- `reports/benchmark_v2_latest.json`

职责：
- 输出 batch 级失败点
- 输出全局通过率与检查数
- 为后续趋势统计保留稳定 JSON 结果

### 5. 说明附录层

文件：
- `APPENDIX_SOURCE_SCHEMA.md`
- `APPENDIX_ICEBERG_METHOD.md`

职责：
- 固化 schema
- 固化构造方法
- 降低后续多人协作时的歧义

## 场景池要求

生产级 benchmark 不应只覆盖程序员协作。

生产级 benchmark 不应把所有主题硬塞进同一个群聊。

每个群聊应当有自己的主题边界，例如：
- 核心推进群：产品边界、技术实现、长线决策更新
- 发布运营群：公告、灰度、客服答疑
- 会务组织群：会议时间、主持、物料、直播备份
- 压测群：高密度 query/schedule/task/noise 分类

允许长线多任务并行，但应满足：
- 仍围绕同一群的主主题展开
- 长线召回优先在同主题群内发生
- 压测用群不与主线长期记忆群混用

最低应覆盖：
- 工程实现协作
- 产品边界讨论
- 算法规则细化
- 发布运营协调
- 客服/业务口径统一
- 会议组织与会务分工
- 对外答辩或路演准备
- 长线更新与历史追问

## 样例质量要求

每个 batch 应满足：
- 有明确 `scenario`
- 有 `tags`
- 有 `messages`
- 有结构化 `expected`
- 有 `expected_brief`

优先建议有：
- `expected_write_result`
- 多角色参与
- 群外事件影响
- 不是所有信息都在群里明说

## 当前状态

当前 v2 已达到：
- source/runtime 分层
- 冰山作为中间构造法
- 20 个 batch
- 多群聊分主题
- 含非工程场景
- 含一句话标准答案
- 已有基础 evaluator/report

当前仍未完全达到：
- 自动语义相似度打分
- 更大规模 case family
- 多租户/多语言/极端脏数据
- 稳定性趋势报表
