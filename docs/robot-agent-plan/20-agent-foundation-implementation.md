# Agent 底座首阶段实施验收

验收日期：2026-11-15。实施依据：[18 号方案](18-picoclaw-dialogue-context.md)。

本阶段已实现文字、语音和 Dashboard 共用的任务底座：接收目标、保存对话、组合技能、执行任务内规则、接收反馈并继续判断，支持澄清、取消和重启后的明确恢复结果。此次结论以自动化测试、模拟技能和本地模型服务夹具为依据。

PicoClaw 固定到 `bbf6893ca7afad27f1d00a0f5a45982a549c6ed6`，只更新主仓库子模块指针；子模块源码和工作树保持干净。Python 使用标准库，Go 复用现有 PicoClaw 依赖，不增加独立外部服务。

## 交付内容

| 范围 | 实现位置 | 已实现的行为 |
| --- | --- | --- |
| 对话进程 | `cmd/openrobotsoul-dialogue/` | PicoClaw 公开 AgentLoop、自有 ContextManager、共享 SessionStore、JSON 行协议与受控关闭 |
| 可靠上下文 | 同上 | 校验和追加日志、原子 Save、半写尾部恢复、请求及结果去重、完整回合摘要、并发提交校验、存储故障阻断决策 |
| 会话与模型隔离 | `robotsoul/language_session.py` | 可信身份生成会话键、严格启动握手、实际工具集为空、拦截工具调用、单一 provider、配置变化后重启 |
| 任务管理 | `robotsoul/task_runner.py`、`task_store.py` | 唯一任务回环、SQLite、澄清与事件等待、独立 reply、先记录再派发、未知动作不重放、结果待发送队列 |
| 规则调度 | `robotsoul/task_rules.py` | 顺序、结果条件、事件等待、有界重试、本地时间线；可注入单调时钟与统一预算 |
| 动作边界 | `robotsoul/runtime.py`、`models.py`、`agent.py` | 复用参数、能力、风险、确认和复核检查；当前任务证据与宿主登记评估器约束完成判断 |
| 本地停止 | 任务运行器、`robotsoul/picoclaw.py`、`adapters/unitree_go2/sdk_driver.py` | 独立取消标记、模型与编码中断、截止计时器、适配器停止确认、派发前取消竞态修复 |
| Dashboard | `robotsoul/dashboard.py`、`robotsoul/web/` | 异步提交、查询、继续、取消；显示进度、答复及待补充问题；账户和登录对话隔离 |
| 语音 | `robotsoul/voice/` | 优先播报 reply、同一对话续接澄清、保持新目标替换语义、提交时固定通话身份 |
| 构建与运行 | `robotsoul/build.py`、`cli.py`、`profiles.py` | 独立 Go 构建、三平台产物、生产任务库路径和配置、retry_safe 登记 |

## 两个模拟场景

### 去阳台检查窗户

`tests/test_task_rules.py` 使用脚本化 Agent 和模拟导航、观察技能验证回环：

- 导航成功之后才调用观察；导航失败只执行导航，后续检查不派发。
- `open` 和 `closed` 都能完成“检查”，答复保留具体窗户状态。
- `unknown` 最多观察三次，之后进入等待用户状态，单独给出补充问题。
- 结果评估器检查导航成功、检查已完成及确定的窗户状态；模型提供的证据引用仍需通过当前任务和命令关联检查。

接入真实设备时，宿主必须登记领域评估器，并通过 `submit_task(..., goal_kind="physical", expected_evaluator="window_check")` 固定完成条件。通用文字答复不构成物理目标达成证据；此次未实现任意自然语言目标的自动领域分类。

### 音频节拍驱动闪灯

同一测试文件用可注入的假播放时钟驱动 128 个灯光事件，逐项核对派发时间；Agent 决策共两次，不为每个节拍请求决策。暂停、跳转、恢复、结束、取消和迟到跳过均有回归测试；跳转后不重放旧事件，迟到事件不集中补发。

`tests/test_dialogue_budget.py` 进一步使用真实 Windows Go 进程验证 256 条命令的完整终态事实和全部 256 个证据 UUID 能在 32768 tokens 配置中完成协议往返。模型输入使用无损表格去除重复字段，SQLite 仍保留原始 768 条生命周期反馈及审计记录。

Go 测试补充了 256 条结果 note 积压后的下一轮普通对话：按完整回合分批摘要，每批校验输入与输出预算，最新两条结果仍进入决策输入。后台上下文摘要与任务 Agent 决策分别管理，不参与灯光时序。

## 可靠性与隔离证据

| 验收项 | 自动化证据 |
| --- | --- |
| 摘要期间追加结果或新回合 | `context_test.go`、`ledger_test.go`：并发追加后只提交覆盖前缀，未摘要尾部保留 |
| 清空、恢复与后台摘要相遇 | 同上：会话代次、摘要版本、完整回合边界校验使过期提交失效 |
| 半写日志、损坏、写入与同步失败 | `ledger_test.go`：仅修复不完整尾部，中部损坏拒绝启动，存储错误锁存 |
| 重复请求与进程重启 | `gateway_test.go`、`test_agent_integration.py`：恢复完整提交，重复返回已提交响应，未提交请求明确中断 |
| 工具隔离与无降级 | `gateway_test.go`、`provider_test.go`、`test_agent_integration.py`：实际 tools=0、非流式请求、工具调用拒绝、自有管理器失败不能降级 |
| 动作派发和任务恢复 | `test_task_store.py`、`test_task_runner.py`：派发前持久化失败不执行，重启后的未知命令不重发 |
| 当前任务证据 | `test_task_runner.py`：拒绝缺失、外部任务证据、模型降级领域评估器及用局部成功遮盖失败 |
| 独立取消与任务截止 | `test_task_runner.py`、`test_task_runtime.py`：模型、复核、编码、事件等待、步骤间与活动动作均覆盖 |
| 适配器停止竞态 | `test_go2_precancel.py`：注册至派发间取消、旧命令停止与后续命令隔离、原生命令与停止顺序 |
| 任务内规则限制 | `test_task_rules.py`：整树预检、未知模板拒绝、命令和时间线累计预算、安全重试最多三次 |
| 用户和入口隔离 | `test_agent_integration.py`：不同身份、入口、登录对话不能串历史或继续原任务；排队语音保留提交时身份 |
| 结果补交 | `test_task_store.py`、`test_task_runner.py`：待发送记录和结果同事务保存，只重发 note，不重执行技能 |
| 兼容性 | 全量 Python 测试：旧顺序计划、直接技能、安全复核、名义完成说明和语音替换继续通过 |

## 实际验证记录

在 Windows 原生环境运行：

```powershell
python -m unittest discover -s tests -q
python -m compileall -q robotsoul adapters/unitree_go2 tests
node --check robotsoul/web/dashboard.js
git diff --check
```

Python 最终全量运行耗时 77.519 秒，共发现 381 项测试：375 项通过，6 项按环境跳过，无失败。跳过项涉及 Windows 不支持的 POSIX 权限检查、符号链接能力及未安装的可选 WebRTC 音频依赖；此次真实 Go 对话进程测试实际运行。

在 `cmd/openrobotsoul-dialogue` 中运行：

```powershell
go test -mod=readonly -count=1 ./...
go vet -mod=readonly ./...
```

两项均通过；最终 Go 测试包运行耗时 6.187 秒。Go 测试覆盖追加日志、摘要并发、批摘要、HTTP provider 协议与常驻子进程重启。另已运行带持久任务库的现有 CLI simulator demo，检查任务完成及关联反馈落库。

三个独立构建均已成功，使用 `CGO_ENABLED=0`、`goolm,stdjson` 与只读 Go 模块；每个目标目录包含独立 `dialogue-manifest.json`：

| 目标 | 构建命令 | 产物 |
| --- | --- | --- |
| Windows amd64 | `python -m robotsoul.build --dialogue-only --platform windows --arch amd64` | `build/windows-x86_64/bin/openrobotsoul-dialogue.exe` |
| Linux amd64 | `python -m robotsoul.build --dialogue-only --platform linux --arch amd64` | `build/linux-x86_64/bin/openrobotsoul-dialogue` |
| Linux arm64 | `python -m robotsoul.build --dialogue-only --platform linux --arch arm64` | `build/linux-aarch64/bin/openrobotsoul-dialogue` |

任务默认预算为 8 次 Agent 决策、32 条普通技能命令、300 秒和 256 个时间线事件。模型默认窗口为 32768 tokens，决策输出预留 8192 tokens；不能容纳必要上下文时明确报错。

## 运行约定与验收边界

- CLI 默认持久任务库为 `~/.openrobotsoul/tasks.sqlite3`，可用 `--task-store` 或 `OPENROBOTSOUL_TASK_STORE` 覆盖。库 API 省略存储路径时使用内存，部署时需明确传入 `task_store_path`。
- 对话日志默认在 `~/.openrobotsoul/dialogue`；可用 `OPENROBOTSOUL_DIALOGUE_HOME` 覆盖。缺失对话二进制会明确报错，不切换到旧无状态 gateway。
- 未确定动作终态改为 `interrupted`；不能将其当作普通失败后自动重试。仍有效观测进入后续快照，事件去重不会把有效事实清空。
- Linux 两个产物完成交叉构建，尚未在 Linux 主机执行。当前 Windows 无 C 编译器，未运行 Go race detector；普通并发回归已执行。
- 模型验证使用显式测试 provider 和本地 HTTP 服务夹具，未连接外部模型账户验收。`--fake-provider` 仅用于测试，生产配置仍需有效 provider。
- 首阶段不包含真实手机播放同步、机器人导航、摄像头和窗户识别，也不包含长期自动任务。实机同步精度、导航成功率、视觉识别与设备停止能力需在对应设备接入时分别验收。

可行性研究与原缺陷证据继续保留在 [19 号报告](19-agent-foundation-feasibility.md)、[原始 18 号文档](evidence/18-original-dialogue-context-plan.md) 和 `evidence/18-dialogue-feasibility/`。
