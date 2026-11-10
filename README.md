# OpenRobotSoul

OpenRobotSoul 是一个以人机交互和受控自我探索为核心、可部署到多种常见机器人型号上的具身运行框架。每个运行时实例连接并控制一台机器人；机器人代码封装在 `adapters/<robot_code>/`，运行目标通过部署配置选择机器人模块。

项目采用云端 AI 做语义理解与任务规划，不要求机器人本地运行大语言模型。本地保留设备驱动、快速运动控制和安全监督。外部感知设备可通过手势、语音和可选的视觉注意力信号触发交互；执行结果和后续感知由事务流融合并评估。

## 核心组成

1. **机器人适配器**：拆分为感知与动作适配器，将设备协议映射到统一事件、能力和反馈契约。
2. **Agent**：基于云端模型理解用户表达、拆解任务并调用受约束的机器人技能。
3. **事务处理流回环**：管理任务状态、交互触发、动作反馈融合、主动行为资格判断和结果评估。

机器人模块目录、配置切换与新增机器人要求见[机器人模块规范](docs/robot-agent-plan/12-robot-module-standard.md)。

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
- [机器人模块组织规范](docs/robot-agent-plan/12-robot-module-standard.md)
- [跨平台 C++ 构建工具](docs/robot-agent-plan/14-build-tool.md)
- [Dashboard 与接口风险门禁](docs/robot-agent-plan/15-dashboard.md)

## 当前状态

Python MVP 已启动，当前包含确定性模拟适配器、两个能力不同的模拟 profile、可替换的 Agent 接口、任务状态回环、参数限制、命令去重和 JSON 审计轨迹。当前 Agent 使用本地规则匹配，模拟器反馈不能证明任何真实硬件行为。

当前 `config/robot.json` 选择 `unitree_go2`，表示 Go2 是正在准备的目标；由于 SDK bridge 仍未实现，默认运行会明确失败，不会回退到模拟器。要运行模拟器需显式指定 `--robot-module simulator`。机器人模块位于仓库根目录 `adapters/`；每个模块拥有自身 profile、driver bridge 和机器人专属 `third_party/`。runtime 不包含机器人型号分支。

PicoClaw 已作为固定提交的 Git 子模块登记在 `third_party/picoclaw`，作为 Pi coding agent 的先行替代方案供隔离工作区编码能力评估；它不是当前 Python MVP 的运行时 Agent。集成约束和验收条件见 [Agent 选型与 PicoClaw 集成决策](docs/robot-agent-plan/11-agent-tooling-decision.md)。

真实机器人与驱动、语音采集/转写、云端模型 provider、记忆、主动行为和恢复监督器仍待后续阶段实现。Unitree Go2 仍是候选实机目标；具体 SDK、连接方式、能力和安全限值必须先按方案文档预研和验证。

## 运行 MVP

要求 Python 3.11 或更新版本，不需要额外运行依赖：

```powershell
python -m robotsoul profiles
python -m robotsoul demo
python -m robotsoul --robot-module simulator demo
python -m robotsoul --robot-module simulator --profile simulator-compact run "抬头"
python -m robotsoul --robot-module simulator dashboard
python -m robotsoul coding-agent check
python -m unittest discover -s tests -v
python -m robotsoul.build --platform linux --arch x86_64
```

`config/robot.json` 必须设置 `robot_module`，`profile_id` 可选；不提供时使用模块默认 profile。CLI 的 `--robot-module` / `--profile` 只覆盖本次运行。配置只能选择静态 registry 中的 ID，不能指定任意 Python import path。命令输出 JSON 任务、反馈和审计记录。每个 runtime 默认将 adapter tool 调用的开始和结束事件追加到 `logs/tool_calls.jsonl`，包括关联 ID、工具名、脱敏参数、返回反馈/错误和耗时；字符串参数值不会持久化。嵌入式运行可通过 `RobotRuntime.from_profile(..., audit_sink=JsonlToolCallAuditSink(path))` 指定其他持久化位置，或注入自定义 sink；测试/本地开发可显式使用 `NullToolCallAuditSink`。详见[工具调用审计记录](docs/robot-agent-plan/13-tool-call-audit.md)。Simulator 模块的 `simulator-basic` 提供 `speech.say`、`head.look` 与受限的 `base.move_distance`；`simulator-compact` 不提供移动能力且头部范围更小。

### Dashboard

本地试用时启动 simulator Dashboard，PIN 至少 8 个字符；未设置环境变量时会在终端安全提示输入：

```powershell
$env:ROBOTSOUL_DASHBOARD_PIN = "替换为本地操作员PIN"
python -m robotsoul --robot-module simulator dashboard
```

浏览器打开 `http://127.0.0.1:8765`。该 HTTP 地址只绑定本机，手机或其他局域网设备不能通过它控制机器人。局域网访问需绑定具体 LAN IP 并提供 TLS 证书，证书需包含该 IP 的 SAN，且手机应信任签发证书的 CA：

```powershell
python -m robotsoul --robot-module simulator dashboard --host 192.168.1.20 --tls-cert cert.pem --tls-key key.pem
```

`confirm` 技能需通过 `RobotRuntime.from_profile(..., safety_reviewer=...)` 配置无工具 SafetyReviewer；未配置或复核失败时会拒绝调用。`high` 技能只允许 Dashboard 操作员调用，并要求重新输入 PIN。当前默认配置仍指向尚无 SDK bridge 的 Go2；示例显式选择 simulator。手机语音通道尚未接入。安全边界、API 和风险级别定义见[Dashboard 与接口风险门禁](docs/robot-agent-plan/15-dashboard.md)。

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

首次使用 Go 核心 CLI 前，使用下方统一构建工具构建 PicoClaw；其二进制会放在 `build/<platform>-<arch>/bin/`，Go 编译缓存和模块缓存位于同一目标目录的 `go/` 下。运行时自动从该路径查找 PicoClaw，也可通过 `OPENROBOTSOUL_PICOCLAW_BINARY` 显式覆盖。使用 `--allow-shell` 才会启用命令执行；PicoClaw 的工作区限制不是完整操作系统沙箱，启用 shell 前须在容器、虚拟机或等效隔离环境中运行。

## Unitree Go2 机器人模块

Go2 全部专属代码放在 `adapters/unitree_go2/`；SDK/驱动放在 `adapters/unitree_go2/third_party/sdk/`。`unitree-go2-development` profile 目前没有声明技能，SDK bridge 接好后也不会执行动作，直到经过实测的能力写入该模块 profile。把 `config/robot.json` 的 `robot_module` 改成 `unitree_go2` 即选择 Go2；SDK bridge 尚未实现时，系统会明确报错并停止。

```python
from robotsoul import RobotRuntime, load_selected_profile
from robotsoul.agent import DemoAgent

profile = load_selected_profile(robot_module="unitree_go2")
runtime = RobotRuntime.from_profile(profile, agent=DemoAgent())
```

也可以在 Python 中嵌入运行时：

```python
from robotsoul import RobotRuntime, load_profile

runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
result = runtime.run_text("请抬头")
print(result.task.status, result.evaluation)
```

模拟适配器可注入带时效和隐私分类的观测；运行时会拒绝过期、未来时间、其他机器人和重复事件 ID，并且只把 `operational` 类观测交给 Agent：

```python
from robotsoul import Observation, RobotRuntime, load_profile
from robotsoul.models import utc_now

profile = load_profile("simulator-basic")
runtime = RobotRuntime.from_profile(profile)
now = utc_now()
runtime.adapter.publish_observation(Observation(
    event_id="event-1",
    robot_id=profile.robot_id,
    device_id="microphone-1",
    event_type="speech.transcript",
    observed_at=now,
    received_at=now,
    payload={"text": "抬头"},
    source="simulator",
    ttl_ms=3000,
    privacy_class="operational",
))
result = runtime.run_text("请抬头", task_id="task-1")
```

`cancel_task(task_id)` 可请求停止正在执行的可中断技能；只有适配器确认停止后，任务才会进入 `cancelled`。

## C++ 构建工具

使用 `python -m robotsoul.build` 同时构建 Unitree SDK2 当前 checkout 中的 Go2 C++ 示例和 PicoClaw Go CLI。CMake 缓存、Go 编译/模块缓存、中间文件、SDK 运行库副本、两个组件的编译后二进制和 `manifest.json` 都放在项目根目录的 `build/<platform>-<arch>/` 下；不会把构建产物写入 SDK 源码或 PicoClaw 子模块目录。

有明确目标时使用参数模式：

```powershell
python -m robotsoul.build --platform linux --arch x86_64 --build-type Release --jobs 4
python -m robotsoul.build --platform linux --arch aarch64 --toolchain path/to/aarch64-toolchain.cmake
python -m robotsoul.build --list-targets
python -m robotsoul.build --platform linux --guide
```

不带参数会启动 CLI 向导，依次选择目标系统、架构和构建类型，并检查 CMake/C++ 编译器。`--guide` 只输出当前平台的依赖安装指引。平台可写 `linux`、`windows` 或 `macos`，架构可写 `x86_64`/`amd64` 或 `aarch64`/`arm64`；跨编译必须额外提供 CMake toolchain 文件。

当前 `adapters/unitree_sdk2` checkout 自带的预编译 DDS 和 Unitree SDK 库只有 Linux `x86_64`、Linux `aarch64` 两套，Windows/macOS 只能得到明确的 SDK 缺失诊断，不能直接链接这些 `.so`；PicoClaw 本身支持 Go 交叉编译，但统一构建要求 C++ SDK 目标也可用。构建目标是 SDK 的 Go2 官方示例和 PicoClaw，不是尚未实现的 RobotHarness Go2 bridge；其中 `go2_low_level` 会涉及底层电机控制，构建成功不代表允许在实机上运行。构建会在执行 Go 代码前拒绝 `go.mod`/`go.sum` 有改动的 PicoClaw 子模块，并以只读模块模式编译。
