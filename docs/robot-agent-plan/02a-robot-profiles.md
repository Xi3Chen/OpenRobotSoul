# 机器人兼容配置

## 目标

机器人 profile 用配置选择一个已实现的适配器组合、连接方式、设备映射和可用能力。对同一适配器/协议族中已支持的型号，新增型号或切换机型应以新增或修改 profile 为主，不在 Agent 和事务处理核心中写型号分支。

一个运行时实例在任一时刻只加载一个 robot profile 并绑定一台物理机器人。更换型号意味着为另一台机器人部署/启动一个实例或切换当前实例配置后重新初始化；这份配置不描述多台机器人的集合，也不提供机器人间协同。

“只改配置”仅适用于已有驱动/适配器能够理解的协议和动作语义。首次接入新的厂商 SDK、传输协议、状态定义或动作语义仍需要适配器代码及实机验证。配置不是自动生成硬件兼容性的机制。

## 配置分层

建议按四层组合，优先级由具体到通用：

1. 基础 schema 默认值：仅放不涉及安全的通用默认。
2. 适配器/协议 profile：驱动类型、协议版本和协议族共性。
3. 机器人型号 profile：型号设备映射、校准引用和能力差异。
4. 部署实例配置：robot_id、网络地址、设备序列号和凭证引用。

后层只可补充或收紧约束。部署配置不得将低于适配器、本地安全监督或厂商硬限制的安全上限调高。密钥使用 secret reference，不以内联明文保存在 profile。

## Profile 字段

| 字段 | 作用 |
|---|---|
| profile_schema_version | 配置 schema 版本，用于迁移和兼容性检查 |
| profile_id / profile_version | 稳定型号 profile 标识和版本 |
| manufacturer / model | 期望厂商与型号，用于展示和连接后校验 |
| adapter_id / adapter_version | 选择已安装的驱动/适配器实现及兼容版本范围 |
| transport | 已实现的传输类型和参数，例如本地 socket、SDK 或 ROS 2；不得填写可执行代码 |
| devices | 传感器、执行器和控制器实例到适配器端点的映射 |
| frames / units | 坐标系、轴向、角度/距离/速度单位和时间基准 |
| calibration_ref | 校准数据版本或受控文件引用 |
| capabilities | 此型号实际支持的规范技能、观测类型和对应限制 |
| required_capabilities | 启动时必须发现的能力，缺失则相关功能不启用或启动失败 |
| optional_capabilities | 可选能力；缺失时从 Agent 工具集隐藏 |
| safety_policy_ref | 本地安全策略引用；模型和 profile 不能覆盖独立的硬件急停/安全控制 |
| health_checks | 连接、控制器、传感器及时间同步检查定义 |
| extension_policy_ref | 允许加载的插件来源、能力和风险级别策略引用 |

不要允许 profile 携带任意脚本、动态库路径、shell 命令或 Python/JavaScript 表达式。新增可执行逻辑应通过受审查、带版本且已安装的适配器插件交付。运行时自动安装/更新仅适用于 extension policy 明确预授权的低风险插件。

## 示例形状

以下 YAML 是字段形状示例，不代表 Unitree Go2 的真实 SDK、Topic、单位、限值或已验证能力：

~~~yaml
profile_schema_version: 1
profile_id: vendor.model.protocol-family
profile_version: 1
manufacturer: Example
model: ExampleRobot
adapter:
  id: example-adapter
  version: ">=1.0 <2.0"
  transport:
    type: configured-driver
    endpoint_ref: robot.example.endpoint
identity:
  expected_serial: null
devices:
  - id: base
    role: locomotion
    endpoint: configured-driver/base
frames:
  base_frame: base_link
  world_frame: odom
  angle_unit: rad
calibration_ref: calibration/example-v1
capabilities:
  - name: base.stop
    required: true
  - name: base.move_distance
    required: false
    limits_ref: safety/example-move
safety_policy_ref: safety/example-robot
secrets:
  endpoint_ref: secret://robot/example/connection
~~~

示例 endpoint 和安全策略是占位符。真实型号 profile 必须来自对应设备的资料、驱动实现和实机验证，不复制示例数值。

## 能力协商与工具暴露

启动过程按以下顺序执行：

1. 解析并校验 profile schema、版本和引用。
2. 只加载本地已安装且版本兼容的 adapter_id。
3. 建立连接并读取设备身份、协议版本、控制器和传感器能力。
4. 对照声明能力和实测能力，验证必需能力、单位、坐标和校准状态。
5. 与本地安全策略取约束交集，生成该 robot_id 的有效能力清单。
6. 只将有效能力映射成 Agent 可见工具；工具执行时再次检查当前状态和安全限制。

设备身份不匹配、必需能力缺失、校准无效或连接不健康时，应拒绝启动对应功能并给出可诊断错误。可选能力缺失时禁用该技能，不得静默套用另一型号映射。安全能力、急停链路和基本停止能力的必需性由部署安全策略决定。

Agent 看到的是稳定的规范技能，例如 base.move_distance 或 head.look，不依赖品牌命名。如果某型号没有头部关节，则不暴露 head.look；如果行走是基于导航目标而不是距离技能，应按已验证的语义提供能力，不把两种动作伪装成等价命令。

## Unitree Go2 接入策略

Go2 可作为首发真实设备候选，但具体接入路径必须在阶段 0 实测确认。比较厂商 SDK、可用 ROS 2 驱动或已有厂商接口时，需要记录：

- 支持的硬件/固件版本、网络拓扑和控制权获取流程。
- 可用的状态反馈、传感器数据、动作接口和停止行为。
- 时间同步、坐标系、单位和校准要求。
- 断连、进程退出、租约过期和急停时的设备行为。
- 驱动许可证、维护状态和部署依赖。

本方案不预写 Go2 的 API、ROS Topic、技能列表或数值限值；这些应在设备与驱动版本明确后写入单独 profile 和 ADR。

## 配置测试与兼容矩阵

每个发布的型号 profile 都应记录 adapter/driver 版本、设备固件范围、支持能力、未支持能力、测试环境和验收日期。兼容矩阵至少包含：

| 型号/profile | Adapter 版本 | 连接验证 | 感知验证 | 动作反馈验证 | 安全测试 | 状态 |
|---|---|---|---|---|---|---|
| simulator-basic | fake-adapter | 自动化 | 回放 | 完整生命周期 | 仿真 | CI |
| Unitree Go2 | 待阶段 0 选型 | 待实测 | 待实测 | 待实测 | 待实测 | 候选 |

profile 的 CI 校验应覆盖 schema、版本兼容、重复 ID、无效引用、非法单位、越界限制和未知 capability。硬件在环测试应覆盖能力协商、型号不匹配、急停和断连。

对“第二型号只改配置”的验收，必须在同一已实现协议/适配器范围内选一个型号进行验证，并证明更换 profile 后 Agent 和事务流无型号专属代码。若选择的新型号协议不同，则验收预期改为新增 adapter 插件但保持核心模块不变。
