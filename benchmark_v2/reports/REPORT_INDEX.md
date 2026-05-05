# benchmark_v2 Reports

当前建议直接看这几份：

- 最新主报告 PDF：
  [INITIAL_BENCHMARK_REPORT.pdf](/Users/davidai/Desktop/feishuai/feishu_ai/benchmark_v2/reports/initial_report/INITIAL_BENCHMARK_REPORT.pdf)
- 同内容新版工作文件：
  [BENCHMARK_VALIDITY_RERUN_REPORT.pdf](/Users/davidai/Desktop/feishuai/feishu_ai/benchmark_v2/reports/reviewed_report/BENCHMARK_VALIDITY_RERUN_REPORT.pdf)
- 轻量全量结果：
  [light_full_results.json](/Users/davidai/Desktop/feishuai/feishu_ai/benchmark_v2/reports/reviewed_report/light_full_results.json)
- 深度子集结果：
  [deep_oc_launch_ops_results.json](/Users/davidai/Desktop/feishuai/feishu_ai/benchmark_v2/reports/reviewed_report/deep_oc_launch_ops_results.json)
- runner 默认最新结果：
  [benchmark_v2_latest.json](/Users/davidai/Desktop/feishuai/feishu_ai/benchmark_v2/reports/benchmark_v2_latest.json)

这版报告已经包含：

- 分层叙事：结构层 / 轻量回归层 / 深度子集层 / 有效性层
- 本轮实际运行内容
- 样本池分类
- 指标分类与解释
- 轻量全量复跑结果
- FULL_WRITE + DEEP_EVAL 子集复跑结果
- “哪些结论能说、哪些不能说”的有效性审查

当前主结论：

- 轻量全量：`21/24` 通过
- 干扰样本通过率：`13/16 = 0.8125`
- multi-intent 通过率：`5/7 = 0.7143`
- near-miss 通过率：`3/6 = 0.5`
- 深度子集 `oc_launch_ops`：`0/3` 通过，失败集中在 realtime 边界和 memory card 命中
