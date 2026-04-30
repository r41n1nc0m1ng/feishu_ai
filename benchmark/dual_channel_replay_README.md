# Dual Channel Replay 说明

这套代码只负责把测试集 JSON 按双通道规则送到当前系统入口。它不负责判断后续链路效果，比如是否生成 MemoryCard、检索是否准确、卡片内容是否好。

## 文件分工

### `full_demo_dual_channel_test.py`

runner，只负责编排顺序：

1. 读取 `full_demo_case.json`。
2. 遍历 `batches`。
3. 对每个 batch，先把 batch 内每条 raw message 依次分配给实时通道。
4. 实时通道全部处理完后，再把整个 raw batch 分配给写入通道。
5. 收集 adapter 返回的结果，做轻量错误检查和 summary。

runner 不直接关心：

- `FeishuMessage`
- `EvidenceMessage`
- `FetchBatch`
- `dispatch_message`
- `segment`
- `CardGenerator`
- Graphiti / SQLite / LLM

如果 runner 里开始 import 具体实时层或写入层入口，说明职责又混回去了。

### `replay_adapter.py`

adapter，负责当前项目的适配层：

- 判断 raw message 是否应该送实时通道。
- 判断 raw message 是否应该进入写入通道。
- 解析 `content` 文本。
- 解析时间。
- 从 fixture sender / mentions 字段里提取当前入口需要的信息。
- raw message -> `FeishuMessage`。
- raw batch -> `FetchBatch[EvidenceMessage]`。
- 调用当前实时入口。
- 调用当前写入入口。

后续如果实时入口或写入入口变化，只改这个文件。

## 当前数据流

```text
full_demo_case.json
  |
  v
DualChannelReplayRunner
  |
  |-- raw message 1 --> adapter.send_realtime_message(...)
  |-- raw message 2 --> adapter.send_realtime_message(...)
  |-- ...
  |
  `-- raw batch ----> adapter.send_write_batch(...)
```

adapter 内部当前对应项目入口：

```text
raw message
  -> FeishuMessage
  -> realtime.dispatcher.dispatch_message(...)

raw batch
  -> FetchBatch[EvidenceMessage]
  -> preprocessor.event_segmenter.segment(...)
```

`segment(...)` 只是当前写入入口占位。后续队友要接真正写入链路时，改 `DualChannelReplayAdapter._default_write_entry()` 或注入新的 `write_entry`。

## JSON 字段保持不变

runner 和 adapter 都按现有 fixture 结构读：

```json
{
  "chat_id": "oc_demo_ai_resume",
  "batches": [
    {
      "batch_id": "batch_001",
      "messages": [
        {
          "message_id": "om_xxx",
          "msg_type": "text",
          "create_time": "2026-04-29 10:00",
          "sender": {"id": "ou_user", "sender_type": "user"},
          "content": "{\"text\": \"...\"}"
        }
      ]
    }
  ]
}
```

可以继续保留旧 fixture 里的 `expected_realtime_results`、`expected_write_result` 等复杂字段；当前 runner 不解释它们。runner 只额外支持一个简单的 `expected` 字段：

```json
{
  "expected": {
    "realtime_actions": ["noop", "query"],
    "write_result_count": 1
  }
}
```

## 当前过滤规则

实时通道：

- 只发送 `msg_type == "text"` 或缺省为 text 的消息；
- content 解析后必须有文本；
- interactive 卡片和空内容会被 adapter 标记为 skipped。

写入通道：

- 排除非文本 / 空文本；
- 排除 `sender.sender_type == "app"` 的机器人消息；
- 排除 `@bot` 查询消息；
- `fetch_start` / `fetch_end` 仍按整个 raw batch 的时间范围设置，模拟真实拉取窗口。

这些规则都在 `replay_adapter.py`，不要放进 runner。

## 后续怎么改

如果实时入口变了：

- 改 `DualChannelReplayAdapter.to_realtime_message(...)`
- 改 `DualChannelReplayAdapter._default_realtime_entry(...)`

如果写入入口变了：

- 改 `DualChannelReplayAdapter.to_fetch_batch(...)`
- 改 `DualChannelReplayAdapter._default_write_entry(...)`

如果 fixture 的 sender、mention、content、time 格式变了：

- 改 `DualChannelReplayAdapter.sender_id(...)`
- 改 `DualChannelReplayAdapter.mentions(...)`
- 改 `DualChannelReplayAdapter.parse_content_text(...)`
- 改 `DualChannelReplayAdapter.parse_timestamp(...)`

如果只是要换一个实验入口，也可以不改源码，直接注入：

```python
adapter = DualChannelReplayAdapter(
    realtime_entry=my_realtime_entry,
    write_entry=my_write_entry,
)
runner = DualChannelReplayRunner(adapter)
```

## 运行方式

在仓库根目录：

```powershell
python -m benchmark.full_demo_dual_channel_test
```

或：

```powershell
python benchmark\full_demo_dual_channel_test.py
```

指定其他 fixture：

```powershell
python -m benchmark.full_demo_dual_channel_test benchmark\full_demo_case.json
```

## 职责边界

这套代码能保证：

- case 被读取；
- batch 顺序被保留；
- 每个 batch 内消息先逐条进入实时通道；
- 整个 batch 后进入写入通道；
- 当前项目入口需要的格式由 adapter 统一生成；
- 后续入口变化时不需要改 runner。

这套代码不保证：

- 后续写入链路完整跑通；
- LLM 生成 MemoryCard；
- Graphiti / SQLite 写入成功；
- 检索答案准确；
- benchmark 指标达标。

这些是后续 evaluator 或队友的链路测试职责。
