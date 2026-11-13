# Unitree Go2 可调用接口审阅清单

这份清单用于逐项决定哪些 SDK 能力可以从 RobotHarness adapter 暴露给上层/Agent。表中接口以当前 `adapters/unitree_sdk2` checkout 为准，不代表已经实现或已通过实机验证。

审阅标记：`暴露` 表示可以设计成受约束的上层能力；`不暴露` 表示不进入 Agent/通用工具接口；`仅本地/维护` 表示只能由本地可信控制器或管理员操作。勾选最终决定前，应先确认目标 Go2 固件支持、参数单位/坐标系、控制权、停止和反馈语义。

`Damp()` 保持不暴露：该调用会让机器人卸力。其余 SportClient 动作已包装进 bridge 和 `unitree-go2-development` profile。普通姿态与限时移动为确认级；特技、步态开关和控制源切换为高风险，只允许 Dashboard。避障位移、服务切换、视频、VUI、配置和低级电机命令仍未包装。

## SportClient

来源：`include/unitree/robot/go2/sport/sport_client.hpp`。下表按 SDK 声明逐个列出，不自动等于同名 Agent skill。

| SDK 调用 | 输入 | SDK 能力/影响 | 你的决定 |
|---|---|---|---|
| `Damp()` | 无 | 切换阻尼状态；调用后机器人卸力 | `[x] 不暴露`（已确认） |
| `BalanceStand()` | 无 | 请求平衡站立模式 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `StopMove()` | 无 | 请求停止高层移动；不是物理急停 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `StandUp()` | 无 | 请求站起 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `StandDown()` | 无 | 请求趴下/降低姿态 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `RecoveryStand()` | 无 | 请求恢复站立，可能显著改变姿态 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Euler(roll, pitch, yaw)` | 三个浮点数 | 请求机身欧拉姿态；单位/范围待核实 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Move(vx, vy, vyaw)` | 三个浮点数 | 高层速度移动；单位、坐标系和有效期待核实 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Sit()` | 无 | 请求坐下 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `RiseSit()` | 无 | 请求从坐姿起身 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `SpeedLevel(level)` | 整数 | 设置速度等级；有效值和速度映射待核实 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Hello()` | 无 | SDK 命名动作；具体动作效果待确认 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Stretch()` | 无 | SDK 命名动作；具体动作效果待确认 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `SwitchJoystick(flag)` | 布尔值 | 切换手柄控制相关状态，可能改变控制源 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Content()` | 无 | SDK 命名动作；具体动作效果待确认 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Heart()` | 无 | SDK 命名动作；具体动作效果待确认 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Pose(flag)` | 布尔值 | 设置/切换 pose 类动作状态；语义待确认 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Scrape()` | 无 | SDK 命名动作；具体动作效果待确认 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `FrontFlip()` | 无 | 前空翻特技动作 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `FrontJump()` | 无 | 前跳特技动作 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `FrontPounce()` | 无 | 前扑类特技动作 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Dance1()` | 无 | 舞蹈动作 1 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Dance2()` | 无 | 舞蹈动作 2 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `LeftFlip()` | 无 | 左翻特技动作 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `BackFlip()` | 无 | 后空翻特技动作 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `HandStand(flag)` | 布尔值 | 设置/切换倒立类动作状态 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `FreeWalk()` | 无 | 自由行走模式/动作 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `FreeBound(flag)` | 布尔值 | 设置/切换自由弹跳步态 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `FreeJump(flag)` | 布尔值 | 设置/切换自由跳跃动作 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `FreeAvoid(flag)` | 布尔值 | 设置/切换自由避障模式；具体行为待确认 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `ClassicWalk(flag)` | 布尔值 | 设置/切换经典步态 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `WalkUpright(flag)` | 布尔值 | 设置/切换直立行走动作 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `CrossStep(flag)` | 布尔值 | 设置/切换交叉步态 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `AutoRecoverSet(flag)` | 布尔值 | 设置自动恢复行为，影响跌倒后的动作策略 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `AutoRecoverGet(flag)` | 输出布尔值 | 查询自动恢复设置 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `StaticWalk()` | 无 | 请求静态行走步态 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `TrotRun()` | 无 | 请求小跑/跑步步态 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `EconomicGait()` | 无 | 请求经济步态 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `SwitchAvoidMode()` | 无 | 切换避障相关运动模式；与避障服务关系待确认 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |

## ObstaclesAvoidClient

来源：`include/unitree/robot/go2/obstacles_avoid/obstacles_avoid_client.hpp`。

| SDK 调用 | 输入 | SDK 能力/影响 | 你的决定 |
|---|---|---|---|
| `SwitchSet(enable)` | 布尔值 | 开启/关闭避障服务 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `SwitchGet(enable)` | 输出布尔值 | 查询避障服务状态 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Move(x, y, yaw)` | 三个浮点数 | 避障客户端移动；API 定义中可选择速度模式 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `UseRemoteCommandFromApi(flag)` | 布尔值 | 切换 API 远程命令来源，可能改变控制源 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `MoveToAbsolutePosition(x, y, yaw)` | 三个浮点数 | 请求绝对位姿目标；坐标系/单位/完成反馈待确认 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `MoveToIncrementPosition(x, y, yaw)` | 三个浮点数 | 请求增量位姿目标；坐标系/单位/完成反馈待确认 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |

## RobotStateClient

来源：`include/unitree/robot/go2/robot_state/robot_state_client.hpp`。

| SDK 调用 | 输入 | SDK 能力/影响 | 你的决定 |
|---|---|---|---|
| `ServiceList(serviceStateList)` | 输出服务列表 | 查询服务名、status、protect | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `ServiceSwitch(name, switch, status)` | 服务名、开关值；输出状态 | 改变机器人服务运行状态 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `SetReportFreq(interval, duration)` | 两个整数 | 设置状态上报参数；单位/范围待确认 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |

## VideoClient、UtrackClient、VuiClient

来源：各客户端的 Go2 专属头文件。VUI 和跟踪接口中，查询/设置分别列出以便独立审阅。

| SDK 调用 | 输入 | SDK 能力/影响 | 你的决定 |
|---|---|---|---|
| `VideoClient::GetImageSample(image)` | 输出字节数组 | 获取相机图像样本；图像是敏感数据 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `UtrackClient::SwitchSet(enable)` | 布尔值 | 开启/关闭 UWB tracking 开关 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `UtrackClient::SwitchGet(enable)` | 输出布尔值 | 查询 UWB tracking 开关 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `UtrackClient::IsTracking(enable)` | 输出布尔值 | 查询是否处于 tracking 状态；不提供目标身份 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `VuiClient::SetSwitch(enable)` | 整数开关值 | 设置语音交互开关 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `VuiClient::GetSwitch(enable)` | 输出整数 | 查询语音交互开关 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `VuiClient::SetVolume(level)` | 整数 | 设置音量；有效值域待核实 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `VuiClient::GetVolume(level)` | 输出整数 | 查询音量 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `VuiClient::SetBrightness(level)` | 整数 | 设置亮度；有效值域待核实 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `VuiClient::GetBrightness(level)` | 输出整数 | 查询亮度 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |

## ConfigClient

来源：`include/unitree/robot/go2/config/config_client.hpp`。配置接口可以改变或删除机身配置，风险独立于普通运动控制。

| SDK 调用 | 输入 | SDK 能力/影响 | 你的决定 |
|---|---|---|---|
| `Set(name, content)` | 配置名、内容 | 写入机身配置 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Get(name, content)` | 配置名；输出内容 | 读取机身配置 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Del(name)` | 配置名 | 删除机身配置 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Meta(name, ConfigMeta)` | 配置名；输出结构化元数据 | 查询配置元数据 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `Meta(name, string)` | 配置名；输出字符串元数据 | 查询配置元数据的字符串形式 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |
| `SubscribeChangeStatus(name, callback)` | 配置名、回调 | 订阅配置变更状态 | `[ ] 暴露` `[ ] 不暴露` `[ ] 仅本地/维护` |

## 遥测订阅（只读候选）

这些不是 Agent 动作，但决定哪些设备数据会进入 RobotHarness observation。表中每项也需要确认是否接入，以及数据是否允许传递给 Agent。

| SDK 订阅/数据 | 数据范围 | 你的决定 |
|---|---|---|
| `subscription::SportModeState` (`rt/sportmodestate`) | 运动模式、IMU、估计位姿/速度、足端和障碍距离字段 | `[ ] 接入` `[ ] 不接入` `[ ] 仅本地诊断` |
| `subscription::LowState` (`rt/lowstate`) | IMU、20 路电机、BMS、足端力、遥控器原始字节和 CRC | `[ ] 接入` `[ ] 不接入` `[ ] 仅本地诊断` |
| `subscription::MotorStates(topic, num)` | 指定 topic 的电机状态数组 | `[ ] 接入` `[ ] 不接入` `[ ] 仅本地诊断` |
| `subscription::MotorCmds(topic, num)` | 指定 topic 的电机命令数组 | `[ ] 接入` `[ ] 不接入` `[ ] 仅本地诊断` |
| 通用 DDS 订阅 `rt/wirelesscontroller` | 摇杆轴与按键；topic 由通用示例使用 | `[ ] 接入` `[ ] 不接入` `[ ] 仅本地诊断` |
| LiDAR / UWB / HeightMap / PointCloud / FrontVideo / Audio 类型 | 消息类型存在，但 Go2 topic 或实机支持尚待确认 | `[ ] 接入` `[ ] 不接入` `[ ] 仅本地诊断` |

## DDS publisher 与低级控制

这些接口不应因出现在 SDK wrapper 中就默认纳入 Go2 adapter。尤其低级命令会直接控制执行器。

| SDK publisher/调用面 | 影响 | 审阅建议/你的决定 |
|---|---|---|
| `publisher::LowCmd` (`rt/lowcmd`) | 发布最多 20 路电机命令，包含位置/速度/增益/力矩等字段 | 默认禁止 Agent 调用；`[ ] 不暴露` `[ ] 仅可信本地控制器` |
| `publisher::MotorCmds(topic, num)` | 向指定 topic 发布电机命令数组 | 默认禁止 Agent 调用；`[ ] 不暴露` `[ ] 仅可信本地控制器` |
| `publisher::WirelessController` (`rt/wirelesscontroller`) | 发布摇杆/按键状态，影响控制输入链路 | 默认禁止 Agent 调用；`[ ] 不暴露` `[ ] 仅可信本地控制器` |
| `publisher::LowState` / `SportModeState` / `MotorStates` | 发布状态类 DDS 消息，可能伪造或干扰状态流 | 不作为产品能力；`[ ] 不暴露` `[ ] 仅测试工具` |
| `unitree::robot::go2::shutdown()` | 释放默认运动模式；SDK 明确要求在发布 `rt/lowcmd` 前调用 | 仅低级控制器生命周期内部；`[ ] 不暴露` `[ ] 仅可信本地控制器` |

## Bridge 内部生命周期（不是 Agent 技能）

以下方法是连接/SDK 管理步骤，不应登记为机器人动作能力：

| 调用 | 用途 | 处理建议 |
|---|---|---|
| `ChannelFactory::Instance()->Init(domain, networkInterface)` | 初始化 DDS transport | 适配器内部初始化；network interface 只能来自受信任部署配置 |
| `ChannelFactory::Instance()->Release()` | 释放 DDS transport | 适配器关闭流程 |
| 各客户端 `Init()` | 初始化客户端 service | 适配器内部初始化 |
| `ClientBase::SetTimeout(timeout)` | 设置 SDK 请求超时 | 固定/受配置约束，不作为 Agent 参数 |
| `GetApiVersion()` / `GetServerApiVersion()` | 检查 SDK 与设备服务端 API 版本 | 连接兼容性诊断；不作为 Agent 技能 |
| `SportClient(enableLease)` / `WaitLeaseApplied()` | 创建运动客户端并等待控制租约 | 适配器内部控制权流程；lease 使用时机待固件核对 |

## 审阅结论记录

| 项目 | 决定/备注 |
|---|---|
| 禁止暴露 | `SportClient::Damp()`：调用后机器人卸力 |
| 其余技能和设备数据 | 等待逐项审阅 |
| 实机验证前 profile | 保持无运动技能；接口审阅结果不能代替限值、反馈和安全行为验证 |
