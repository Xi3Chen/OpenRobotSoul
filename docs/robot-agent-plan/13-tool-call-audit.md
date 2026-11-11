# 工具调用审计记录

## 目的

为工具调用故障的事后分析，runtime 在调用适配器前后写入关联审计事件。事件可将 Agent 选择的 tool、机器人命令、适配器反馈和耗时关联起来；开始和完成分开落盘，以便区分“未调用”“调用失败”和“调用过程中进程退出”。

## 事件契约

每个 `call_id` 至少有一条 `started` 事件。适配器正常返回或抛异常时追加一条 `completed` 事件。字段包括：

- `event_id`、`call_id`、`phase`、`timestamp`、`duration_ms`
- `task_id`、`trace_id`、`command_id`、`robot_id`
- `tool`、`method`、`deadline`、profile capability 的 timeout/interruptible 声明
- 调用参数摘要，以及返回反馈状态、message、error_code、evidence 或异常类型/摘要

JSONL 中只有 `started` 而缺少 `completed` 的 `call_id` 表示调用未留下终态记录，应结合设备状态和 adapter 日志判断是否已产生物理效果。`completed` 表示 adapter 已返回，不等同机器人动作已完成；成功仍须依据反馈生命周期和观测证据判定。

## 持久化与注入

- 每个 runtime 默认追加到 `logs/tool_calls.jsonl`，每个事件单独一行，并在写入后 flush/fsync。
- `logs/` 是运行数据，不纳入版本控制。
- 嵌入式调用可将 `JsonlToolCallAuditSink(path)` 传给 `RobotRuntime.from_profile(..., audit_sink=...)`，或实现 `ToolCallAuditSink.write(event)` 注入外部审计后端。
- 不需要持久化时可显式使用 `NullToolCallAuditSink`；测试可用 `InMemoryToolCallAuditSink`。

## 隐私和完整性

- 任务原文不进入 tool-call 文件。
- 字符串参数记录为脱敏标记和长度；数值与布尔参数记录原值，因为它们通常是定位运动故障所需的调用值。
- 参数名含 password、secret、token、credential、authorization 或 api-key 等敏感标记时，值会被脱敏。异常和返回文本会移除与字符串参数完全相同的值及常见凭证模式。
- 返回值中的二进制不写入日志；超大集合和长文本会截断。
- 默认 JSONL 文件以仅当前用户读写权限创建；运行目录仍应按设备数据策略保护并定期轮转。
- 普通动作的 `started` 事件写入失败时，runtime 拒绝开始该 tool，避免发生无法审计的动作。取消/停止调用属于 fail-safe 路径，即使开始事件写入失败仍会尝试停止机器人。完成记录写入失败不会抹掉或伪造 adapter 的实际返回，普通动作的问题会添加到本次 `TaskResult.history` 的 `tool.audit_write_failed` 事件中。

该机制记录的是 Python adapter tool 边界。Go2 C++ bridge 当前提供只读状态接口，不发动作调用；后续如需 DDS transport 诊断或设备端原始日志，应在 bridge 内增加结构化事件，不应将高频遥测全量写入同一个动作审计文件。
