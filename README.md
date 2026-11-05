# OpenRobotSoul

OpenRobotSoul 是一个以人机交互和受控自我探索为核心、可部署到多种常见机器人型号上的具身运行框架。每个运行时实例连接并控制一台机器人；更换机器人型号时，通过已验证的机器人 profile 选择适配器、设备映射和能力。

项目采用云端 AI 做语义理解与任务规划，不要求机器人本地运行大语言模型。本地保留设备驱动、快速运动控制和安全监督。外部感知设备可通过手势、语音和可选的视觉注意力信号触发交互；执行结果和后续感知由事务流融合并评估。

## 核心组成

1. **机器人适配器**：拆分为感知与动作适配器，将设备协议映射到统一事件、能力和反馈契约。
2. **Agent**：基于云端模型理解用户表达、拆解任务并调用受约束的机器人技能。
3. **事务处理流回环**：管理任务状态、交互触发、动作反馈融合、主动行为资格判断和结果评估。

记忆和受控改进是运行能力，不是绕过这三部分的独立控制通道。低风险且已预授权的插件变更在沙箱测试、权限检查和 canary 通过后可自动部署；其他插件、唤醒算法或核心代码变更须经用户/维护者批准。

## 交互与自主能力

- **感知交互**：例如挥手后开启短时语音采集，或将视觉注意力作为可选信号与语音共同满足唤醒条件。摄像头注视不是身份认证。
- **记忆与改进**：保留用户明确授权的偏好和最小必要的交互结果；Agent 可提出唤醒策略调整，经过回放/影子测试、批准和版本回滚流程后生效。
- **主动行为**：在电量、健康、任务状态、环境和用户偏好都满足本地策略时，可更新地图、记录经同意的熟人信息、低频问候或受限探索；低电量时优先返航/充电，不可用时进入 savepower 并请求帮助。
- **自恢复**：独立启动监督器检测版本健康；失败时回退到已知良好版本并进入安全恢复模式，云端模型可协助诊断和准备修复提案。

## 关键边界

- 不支持单个实例控制多台机器人或机器人间协同。
- 已实现适配器覆盖的型号可主要通过 profile 配置切换；新协议或新动作语义仍需适配器代码。
- 云端模型不输出电机级控制，不承担急停、避障或其他实时安全闭环。
- Agent/代码生成器不能直接修改生效中的安全策略；唤醒方式、探索区域等变更需批准，只有受预授权范围限制的低风险插件才可自动部署。
- MVP 不训练或部署端到端 VLA；视觉/语言理解后调用技能属于多模态具身 Agent 流程。

## 项目方案

详细目标、架构、契约、实施阶段和验收标准见 [项目方案文档](docs/robot-agent-plan/README.md)。

- [总体架构](docs/robot-agent-plan/01-architecture.md)
- [机器人兼容配置](docs/robot-agent-plan/02a-robot-profiles.md)
- [事务处理流与数据回环](docs/robot-agent-plan/04-transaction-loop.md)
- [记忆与受控自我改进](docs/robot-agent-plan/07-memory-improvement.md)
- [主动行为策略](docs/robot-agent-plan/08-proactive-behavior.md)
- [插件扩展与代码变更生命周期](docs/robot-agent-plan/09-extension-lifecycle.md)
- [启动监督与自恢复](docs/robot-agent-plan/10-boot-recovery.md)
- [Agent 选型与 PicoClaw 集成决策](docs/robot-agent-plan/11-agent-tooling-decision.md)

## 当前状态

Python MVP 已启动，当前包含确定性模拟适配器、两个能力不同的 robot profile、可替换的 Agent 接口、任务状态回环、参数限制、命令去重和 JSON 审计轨迹。当前 Agent 使用本地规则匹配，模拟器反馈不能证明任何真实硬件行为。

PicoClaw 已作为固定提交的 Git 子模块登记在 `third_party/picoclaw`，作为 Pi coding agent 的先行替代方案供隔离工作区编码能力评估；它不是当前 Python MVP 的运行时 Agent。集成约束和验收条件见 [Agent 选型与 PicoClaw 集成决策](docs/robot-agent-plan/11-agent-tooling-decision.md)。

真实机器人与驱动、语音采集/转写、云端模型 provider、记忆、主动行为和恢复监督器仍待后续阶段实现。Unitree Go2 仍是候选实机目标；具体 SDK、连接方式、能力和安全限值必须先按方案文档预研和验证。

## 运行 MVP

要求 Python 3.11 或更新版本，不需要额外运行依赖：

```powershell
python -m robotsoul profiles
python -m robotsoul demo
python -m robotsoul --profile simulator-compact run "抬头"
python -m robotsoul --profile simulator-basic run "前进"
python -m robotsoul coding-agent check
python -m unittest discover -s tests -v
```

命令会输出完整 JSON 结果，包括 task/trace/command IDs、任务状态、动作生命周期反馈、完成证据和审计事件。`simulator-basic` 提供 `speech.say`、`head.look` 与受限的 `base.move_distance`；`simulator-compact` 不提供移动能力且头部范围更小。未知意图、未声明能力和越界参数会在动作提交前失败。

## PicoClaw 编码任务

PicoClaw coding agent 作为独立 Go 子进程运行，只在仓库之外的临时目录或 Git worktree 中工作，不进入机器人动作 Agent 的事务循环。默认只允许工作区文件读写，不启用 shell、MCP、内置联网工具、定时任务、hooks 或自进化；调用前需先配置模型，并在 PicoClaw 的 `config.json` 中显式设置 `hooks.enabled` 和 `evolution.enabled` 为 `false`。模型 API 链路仍需联网。未找到配置或安全开关未显式关闭时，宿主会在启动 PicoClaw 前拒绝任务。

首次配置可运行 `picoclaw onboard`，随后编辑默认配置 `%USERPROFILE%\.picoclaw\config.json`，配置模型并确认包含：

```json
{
  "hooks": { "enabled": false },
  "evolution": { "enabled": false }
}
```

如配置文件放在其他位置，运行任务前设置 `$env:PICOCLAW_CONFIG` 指向该文件。宿主会将配置和 `.security.yml` 复制到临时目录供该任务使用，并在任务结束后清理副本。

先运行 `python -m robotsoul coding-agent check` 可检查二进制、模型配置和安全开关。检查只验证配置存在性，不会连接模型服务或验证凭证；确认显示 `ready_to_launch: true` 后，再使用 `coding-agent run` 执行隔离工作区任务。

```powershell
# 使用包含目标源码的已提交版本创建独立 worktree
git worktree add ..\RobotHarness-coding <源码提交>
python -m robotsoul coding-agent run --workspace ..\RobotHarness-coding "检查项目并修复测试失败"
```

首次使用 Go 核心 CLI 时，先构建 `third_party/picoclaw/build/picoclaw.exe`。使用 `--allow-shell` 才会启用命令执行；PicoClaw 的工作区限制不是完整操作系统沙箱，启用 shell 前须在容器、虚拟机或等效隔离环境中运行。

也可以在 Python 中嵌入运行时：

```python
from robotsoul import RobotRuntime, load_profile

runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
result = runtime.run_text("请抬头")
print(result.task.status, result.evaluation)
```
