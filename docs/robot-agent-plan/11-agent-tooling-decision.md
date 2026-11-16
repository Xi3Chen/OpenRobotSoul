# Agent 选型与 PicoClaw 集成决策

## 决策状态

自然语言规划由常驻对话进程 `openrobotsoul-dialogue` 完成。它链接 Git 子模块 `third_party/picoclaw` 的 `pkg/agent`（`AgentLoop`、session、上下文压缩），不启动 `cmd/picoclaw` CLI，也不改子模块源码。Dashboard 文本任务、本地语音最终转写和 Dashboard 通话最终转写都交给它做语义理解、多轮对话和技能选择。不再为这些入口使用本地关键词表。

Go 版 PicoClaw 仍有两条用途，工具面分开：

1. **对话**：`openrobotsoul-dialogue` 通过 Go `replace` 导入 `third_party/picoclaw`，持有 session 历史与摘要；工具全部关闭；只能返回已注册技能、拒绝理由或转出编码任务。技能由 Python 运行时校验并执行。
2. **编码**：Python 宿主继续用 `picoclaw agent --message ... --session ...` 启动一次性编码子进程，限制在隔离工作区。

自然语言会话只能请求当前 profile 已注册的技能，由 Python 运行时校验 schema、调用者、风险和确认要求后再执行。编码任务保持下面的工作区隔离，不能借同一次会话去控制机器人。自动部署、canary 和回滚仍受阶段 8/9 的验收门禁约束。本决策不授予对话进程或编码进程直接访问机器人设备、用户记忆或运行时主目录的权限，也不允许它们绕过事务流改写安全策略。编码进程可以读取生产模型凭证（见下文），因为没有凭证它无法调用模型；这不扩大它对机器人和用户数据的权限。

DemoAgent 不再做自由文本关键词匹配，只保留给没有模型配置时的接口测试。本地直接调用只有 `/技能名` 或 `/技能名 参数=值`。它不是 Dashboard、实机或语音对话的规划器。

实现步骤与验收见 [对话上下文与 PicoClaw AgentLoop](18-picoclaw-dialogue-context.md)。

## 集成形态

项目宿主以 Python 为主；PicoClaw 源码固定在 `third_party/picoclaw`，当前固定提交记录于主仓库的 submodule gitlink。PicoClaw 是源码依赖，不是 Python 包；Python 进程不会直接导入 Go 源码。

### 对话进程（链接子模块包）

仓库内独立 Go 模块 `cmd/openrobotsoul-dialogue` 用：

```go
replace github.com/sipeed/picoclaw => ../../third_party/picoclaw
```

构建产物为 `build/<platform>-<arch>/bin/openrobotsoul-dialogue`。主进程启动时由 `LanguageSession` 拉起，退出时关闭。协议仍是一行一个 JSON（`decide` / `note` / `review`）。模型、地址与密钥来自 `config/llm.json` 生成的临时 PicoClaw `Config`，不读取 `~/.picoclaw/config.json`。

对话进程调用子模块的公开 API：`agent.NewAgentLoop`、`ProcessDirect`，并通过公开的 `RegisterContextManager` 注册自有 ContextManager 与共享 SessionStore（追加日志、完整回合摘要）。不使用 PicoClaw 的 legacy 摘要，因为它在并发追加时会丢消息，见 [19 号评估](19-agent-foundation-feasibility.md)。技能表与身份写入受控引导文件进入系统提示；用户原话与助手 JSON 进入 session 历史。任务结束后用 `note` 写入执行结果，不再调模型。会话键由宿主按机器人、入口、主体和对话生成不透明键，不使用裸 `voice` / `call` / `dashboard`，避免串上下文，见 [18 号文档](18-picoclaw-dialogue-context.md)。

对话配置强制关闭全部工具、hooks、evolution、heartbeat 与 MCP；`max_tool_iterations=1`。模型若返回工具调用，本轮拒绝执行。这是首阶段的过渡实现，目标形态是原生 tool-calling，见下文「工具与 skill」。

### 编码进程（CLI 子进程）

Python 宿主通过 `picoclaw agent --message ... --session ...` 启动单次 coding task 子进程；每个任务有唯一的 task/session ID、独立工作目录和硬超时，宿主记录退出码、标准输出、错误输出及超时状态。PicoClaw stdout 是人类可读文本而非 JSON API；结构化结果由 Python 宿主封装。上游 Gateway HTTP 面向聊天渠道集成，不用于本地任务桥接。

编码侧文件工具限制在指定工作区，Shell、MCP、web、后台任务和非必要工具默认关闭。Python 宿主以最小环境启动 PicoClaw，将用户 config/secret 复制到任务临时 home 并在进程结束后清理；HOME、TEMP 等用户目录也重定向到该临时目录。若 PicoClaw hooks 或自进化配置启用则拒绝启动。工作区必须位于项目仓库之外，通常使用单独 Git worktree。工作区路径检查与 PicoClaw 文件工具限制不构成 OS 沙箱；shell 仅可在容器/虚拟机等效隔离已落实后通过显式参数开启。当前集成依赖配置文件中的 API key；基于用户目录的 OAuth 登录凭证暂不继承。任何生成结果均须经静态检查、依赖/许可检查、契约测试和风险门禁。

## 自然语言任务

文本框、本地语音最终转写和通话最终转写都进入 `run_text()`，再交给常驻对话进程。请求里的每个技能带有来自 SDK 的动作介绍，而不只是技能 ID。它只返回一个已注册技能、一个顺序计划、一句拒绝理由，或一条转给编码进程的编程任务。事务流继续校验技能；模型超时、断网或无法匹配已注册技能时，任务失败并保留这次理由，不退回关键词匹配。

`normal` 技能选中后直接校验执行。`confirm` 技能再经同进程的无工具复核（`review`，不写入对话 session），拒绝时使用复核理由。`high` 技能仍不能由自然语言执行。这次会话不启用文件编辑、shell、MCP、hooks 或自进化。语音侧继续负责采集、唤醒、端点检测和转写。

编码进程不随主进程启动。对话进程返回 `coding_task`，或机器人适配器、语音、常驻对话进程启动失败时，宿主才启动一次现有编码进程。没有 `OPENROBOTSOUL_CODING_WORKSPACE` 时只记录请求，不在仓库内改代码。

历史说明：早期实现曾用纯 Python 的 `robotsoul.language_gateway` 做无状态单轮决策。该入口由链接 `third_party/picoclaw` 的常驻对话进程接替，以恢复多轮上下文与压缩；Python 侧仍负责协议封装、技能校验与运行时执行。

## PicoClaw 依赖策略与退出条件

决策（MVP 期间）：薄依赖加自研。PicoClaw 只承担 AgentLoop 外壳、系统提示拼装和 provider 工厂；上下文与会话存储、任务回环、传感器与事件续接、主动触发资格均由本项目自有代码承担。不 fork，不修改子模块源码。

理由：

- [19 号评估](19-agent-foundation-feasibility.md) 复现的缺陷（裸会话键串历史、reaction 工具残留、异步摘要丢消息）都能通过公开扩展点绕过，没有必须改源码的理由。fork 会让项目长期背着补丁追一个快速迭代、v1.0 前不建议生产使用的上游，还会扩大本来要关闭的表面（工具、渠道、MCP、自进化）。
- 传感器推送和主动探索不是 PicoClaw 的设计目标，也不应由它承担。它们属于宿主层：观测管线负责来源、有效期、隐私和去重；`resume_task(event=...)` 把事件续到同一任务；主动行为由本地资格策略放行，云端 Agent 只能提议。

边界：JSON 行协议（hello、decide、resume、note、review、clear、shutdown）是唯一边界。Python 侧不依赖任何 PicoClaw 概念。补一套协议契约测试，任何对话进程实现都必须通过同一套测试，使切换成本保持较低。

升级规则：固定提交。每次升级重跑 19 号的 6 个探针以及 Go 与 Python 回归，并同步更新证据。

退出条件：满足任一项即评估改为自研对话循环。

1. 低延迟方案要求的流式增量决策无法套用现有的工具与 ToolCalls 守卫。
2. 目标部署板（如 Mi Max 2，ARM64，约 3.5GB 内存）上 Go 对话进程的内存或启动时间不达标。linux/arm64 产物目前尚未在 Linux 主机上执行过。
3. 出现第三个只能通过非公开接口绕过的缺陷。
4. 单次升级加回归的耗时超出团队可接受的预算。

## 工具与 skill

目标形态：对话进程里的 agent loop 使用原生 tool-calling，工具做成 loop 最顺手的样子，而不是每轮输出一段 JSON 决策再由宿主解析。工具分两类。

- 机器人动作统一做成 skill。每个 skill 由设备适配器或 profile 声明：参数 schema、前置条件、执行情景（该设备上这个动作怎样算开始、进行、完成）、完成证据、可中断性、风险等级和超时。同一个动作名在不同设备上的执行情景可能不同，所以情景属于 skill 声明，不写进 Agent 提示词。skill 不在对话进程内执行：Agent 只提出调用，TaskRunner 和 Runtime 仍校验参数、能力、风险和确认，并经本地安全检查后才派发。
- 通用 agent 工具不触碰硬件：读取授权记忆、查询任务状态、取消任务、创建 proposal 等，直接作为 agent loop 的原生工具。

过渡：当前实现是 tools=0 加单一 JSON 决策（`act`、`run_rule`、`wait`、`complete`、`clarify`、`reject`、`coding_task`），provider 包装层拒绝非空工具声明和模型 ToolCalls，见 [18](18-picoclaw-dialogue-context.md) 与 [20](20-agent-foundation-implementation.md)。这是首阶段现状，不阻塞阶段 2A 的首次真实端到端测试。切换到原生 tool-calling 需要同时改三处：上文「对话配置强制关闭全部工具」与 `max_tool_iterations=1`，18/19/20 中 tools=0 与 ToolCalls 守卫的约束，以及重跑 19 号的探针。

对退出条件的影响：改成原生 tool-calling 后，退出条件 1 里「现有工具与 ToolCalls 守卫」的前提不再成立，需按新形态重新评估。切换时机待用户确认。

## 验收条件

- 固定 PicoClaw 上游提交和构建方式，在项目支持的目标平台可重复构建；`third_party/picoclaw` 源码无本地脏改动。
- Dashboard 文本、本地语音最终转写和通话最终转写进入同一自然语言入口，并由 `openrobotsoul-dialogue`（`third_party/picoclaw` AgentLoop）对照已注册技能规划；同一 session 的后续话轮能看到必要历史或摘要。
- 对话进程不能编辑文件、执行 shell 或调用未注册技能；编码会话不能调用机器人技能。
- Python 宿主能启动、监控、超时和关闭对话常驻进程与编码子进程；故障、取消及退出码可追踪到任务 ID。
- 编码子进程使用独立临时 home/config/session；允许把生产模型凭证复制进临时 config（已放开读取），但不整体继承主机环境变量，临时副本在任务结束后清理，凭证不进日志和任务产物。
- Agent 能在临时工作区完成代表性文件读取、编辑、测试和补丁输出任务。
- 越出工作区的文件访问、未声明网络和未经允许的命令会被隔离层拒绝。
- 工具调用与结果可结构化关联，不能把工具受理当作代码验证通过或部署成功。
- 生成变更继续经过插件风险分类、授权、canary 和回滚流程。

编码任务在上述隔离验收完成前只用于离线评估，不接入自动部署路径。自然语言规划可以进入运行时，但仍不能绕过事务流和本地安全检查。上游 README 提示项目仍处快速开发阶段，并建议 v1.0 前不要用于生产部署；因此需要固定提交并在升级时重新执行兼容性、安全和回归评估。

## 上游来源

- 项目：<https://github.com/sipeed/picoclaw>
- 固定提交：`bbf6893ca7afad27f1d00a0f5a45982a549c6ed6`
- 上游许可证：MIT；更新版本时仍需复核许可证、依赖和构建产物。
