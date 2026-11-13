# Unitree Go2 SDK 接口盘点

本文根据当前工作区中的 Unitree SDK2 源码盘点 Go2 可接入的接口，为 `unitree_go2` 模块后续 C++ bridge 设计提供依据。它记录的是 SDK 声明和示例，不表示对应功能已在本项目实现、已连接 Go2 实机或已通过安全验证。

## SDK 来源与范围

| 项目 | 当前检查结果 |
|---|---|
| SDK 目录 | `adapters/unitree_sdk2/`，作为多个 Unitree 机型共用的 SDK checkout |
| SDK Git revision | `8e1b927586712edd6a4c29d50dd52dff1e163439`（2026-10-30） |
| CMake 项目版本 | `unitree_sdk2 2.0.0`，使用 C++17 |
| README 声明的预构建目标 | Ubuntu 20.04、GCC 9.4、`x86_64` / `aarch64` |
| Go2 客户端 API 版本 | Sport `1.0.0.1`；Robot State `1.0.0.1`；Video `1.0.0.0`；VUI `1.0.0.0`；Obstacle Avoid `1.0.0.2`；Config `1.0.0.1`；Utrack `1.0.0.1` |

Go2 专属的 C++ bridge、参数转换和 RobotHarness 契约映射应放在本目录；共用 SDK 源码继续放在 `adapters/unitree_sdk2/`。Python 适配器仍通过 `UnitreeGo2Driver` 隔离厂商接口，SDK 类型不应泄漏到 `robotsoul` 核心。

主要源码位置：

- 高层运动：`adapters/unitree_sdk2/include/unitree/robot/go2/sport/`
- 服务客户端：`adapters/unitree_sdk2/include/unitree/robot/go2/{robot_state,video,vui,utrack,obstacles_avoid,config}/`
- Go2 DDS wrapper：`adapters/unitree_sdk2/include/unitree/dds_wrapper/robots/go2/`
- Go2 消息定义：`adapters/unitree_sdk2/include/unitree/idl/go2/`
- 官方调用样例：`adapters/unitree_sdk2/example/go2/`

## 接口分层和建议

| 优先级 | 建议封装 | RobotHarness 中的用途 | 暴露策略 |
|---|---|---|---|
| P0 | SDK 初始化、服务 API 版本、连接健康、状态订阅 | 适配器连接、健康检查和遥测 | 先做只读和诊断；接口存在不等于已实现连接检测 |
| P0 | `LowState`、`SportModeState` | IMU、运动模式、电池、电机和足端等观测 | 只读；检查数据新鲜度、CRC/丢包和单位后再规范化 |
| P1 | `StopMove`、站立/卧倒/坐下 | 基础运动技能和本地停止请求 | 已进入 development profile；`StopMove` 不是硬件急停 |
| P1 | `Move(vx, vy, vyaw)` | 有限时长的底盘速度控制 | 已进入 development profile；按 SDK 公布范围限幅，本地 200–2000 毫秒后调用 `StopMove` |
| P2 | 障碍物避让移动、速度等级、步态、姿态控制 | 受控导航或运动模式 | 单独能力、默认关闭；先验证坐标系、单位、控制权和中断语义 |
| P2 | Video、VUI、Utrack、配置和服务控制 | 感知数据、设备设置及维护 | 根据隐私和权限单独启用，不与一般运动技能混为一组 |
| P3 | `rt/lowcmd` 和逐电机控制 | 研究、硬件实验或受信任的本地控制器 | 不提供给 Agent；需单独的实时、安全与控制权设计 |
| 高风险 | 翻滚、跳跃、倒立、舞蹈等特技 | SDK 示例/厂商动作 | 已包装，只允许 Dashboard 操作员确认；不是普通 Agent 技能 |

当前 `profiles/unitree-go2-development.json` 声明 bridge 已包装的 SportClient 动作。未列入 profile 的 SDK API 不会变成技能。

## 高层运动与模式

`SportClient` 位于 `sport/sport_client.hpp`，继承通用 `Client`。初始化示例先用 `ChannelFactory::Instance()->Init(0, networkInterface)` 初始化 DDS，再设置 timeout 并调用 `Init()`。`SportClient` 构造函数支持 `enableLease` 参数；是否需要 lease、如何与遥控器/机身默认控制器协调，需要按目标固件验证。

| SDK 方法 | 能力含义（按名称/样例） | 建议 |
|---|---|---|
| `StopMove()` | 请求停止当前高层移动 | 本地停止命令的候选；不能替代物理急停，停止必须结合速度状态反馈判定 |
| `StandUp()`、`StandDown()` | 站立、卧倒 | 独立技能；检查周边和当前模式，动作完成由状态反馈判断 |
| `Sit()`、`RiseSit()` | 坐下、从坐姿起身 | 单独验证支持固件和前置状态 |
| `RecoveryStand()` | 恢复站立 | 可能改变倒地机器人的姿态；需要人工/安全策略授权 |
| `Move(float vx, float vy, float vyaw)` | 高层速度指令 | SDK 签名没有标单位、坐标系或持续时间；不可从示例数值推导安全范围 |
| `BalanceStand()` | 平衡站立 | 受控模式切换，是否暴露待逐项审阅 |
| `Damp()` | 阻尼模式 | 会让机器人卸力；按用户决定禁止暴露，不得注册为技能 |
| `Euler(roll, pitch, yaw)` | 姿态角目标 | 角度单位、有效范围和模式前置条件未在此接口声明中说明，暂不对 Agent 开放 |
| `SpeedLevel(level)` | 速度等级 | 等级范围和速度映射待固件/官方文档确认 |
| `StaticWalk()`、`TrotRun()`、`EconomicGait()`、`SwitchAvoidMode()` | 步态或避障模式切换 | 模式编码、默认状态、与避障客户端的关系需要实机核对 |
| `AutoRecoverSet(bool)`、`AutoRecoverGet(bool&)` | 自动恢复开关和查询 | 属于安全相关配置；需明确策略所有权，不允许 Agent 随意切换 |
| `SwitchJoystick(bool)` | 手柄控制切换 | 会影响控制源，需和 API 控制权统一管理 |

客户端还声明了 `Hello`、`Stretch`、`Content`、`Heart`、`Pose`、`Scrape`、`FrontFlip`、`FrontJump`、`FrontPounce`、`Dance1/2`、`LeftFlip`、`BackFlip`、`HandStand`、`FreeWalk`、`FreeBound`、`FreeJump`、`FreeAvoid`、`ClassicWalk`、`WalkUpright` 和 `CrossStep` 等动作或特殊步态接口。SDK 头文件声明只证明有调用入口，不证明目标固件支持、动作安全、可打断或可确认完成；MVP 中默认禁用。

逐项暴露审阅表见[Go2 可调用接口审阅清单](CALLABLE_API_REVIEW.md)。

重要版本差异：`example/go2/go2_trajectory_follow.cpp` 调用了 `SportClient::TrajectoryFollow()`，但当前 checkout 的 Go2 `sport_client.hpp` 没有这个方法声明。不要以该示例为依据实现轨迹跟踪；需先确认 SDK revision/头文件是否配套，或确认 API 是否已移除。

## 障碍物避让与位置移动

`ObstaclesAvoidClient` (`obstacles_avoid/obstacles_avoid_client.hpp`) 声明：

- `SwitchSet(bool)` / `SwitchGet(bool&)`：避障功能开关。
- `Move(x, y, yaw)`：避障服务下的移动请求。
- `MoveToIncrementPosition(x, y, yaw)` / `MoveToAbsolutePosition(x, y, yaw)`：相对位姿/绝对位姿目标。
- `UseRemoteCommandFromApi(bool)`：选择是否由 API 提供远程命令。

对应 API 定义给出 Move 的 `mode` 编码：`0` 为速度、`1` 为增量位姿、`2` 为绝对位姿。公开客户端方法没有直接暴露 mode，但坐标单位、参考坐标系、目标完成反馈、取消方式和速度限制均未由头文件说明。位置移动与避障开关只能在固件支持和控制源切换行为得到验证后单独开放，不能当作 Nav2 或已验证的安全避障保证。

## 只读遥测和设备状态

Go2 的 DDS wrapper 给出 topic 与部分便捷访问器：

| topic / 消息 | SDK 字段 | 可整理为的观测 | 注意事项 |
|---|---|---|---|
| `rt/sportmodestate` / `SportModeState_` | stamp、error_code、IMU、mode、progress、gait_type、position、body_height、velocity、yaw_speed、range_obstacle、foot_force、foot_position_body、foot_speed_body、path_point | 运动模式、估计位姿/速度、姿态、足端与障碍距离摘要 | 位置坐标系、单位、障碍数组方向/单位、progress 语义需确认；消息带时间戳，应做过期检查 |
| `rt/lowstate` / `LowState_` | IMU、20 个 `MotorState_`、BMS、foot_force、foot_force_est、tick、40-byte wireless_remote、CRC | IMU、关节位置/速度/估算力矩/温度/丢包、电量与手柄状态 | 20 个 motor slot 与 Go2 具体关节的顺序要用目标固件核对；勿把无线遥控原始字节外发 |
| `rt/wirelesscontroller` / `WirelessController_` | lx、ly、rx、ry、keys | 手柄输入状态 | 官方通用样例订阅该 topic；摇杆轴向、取值范围和按键映射需确认 |

`IMUState_` 字段包括 quaternion、gyroscope、accelerometer、rpy、temperature。`MotorState_` 包括 mode、q/dq/ddq、tau_est、raw 值、temperature、lost。`BmsState_` 包括 status、soc、current、cycle、温度数组和 cell_vol；原始单位/枚举没有在这些消息头文件中定义，先保留原始数值并记录单位未知，不要猜测转换。

Go2 SDK 还带有以下消息定义，可作为后续传感器接入候选：

| 消息 | 字段摘要 | 接入状态 |
|---|---|---|
| `LidarState_` | 版本、错误状态、点云频率/丢包/大小/扫描数、IMU 和串口统计 | 消息定义可见；当前 Go2 wrapper 没有为它提供默认订阅 topic，topic 和实机可用性待确认 |
| ROS2 `PointCloud2_` | ROS PointCloud2 格式点云 | Go2 wrapper 引用了类型，但当前 SDK 搜索未确认 Go2 点云 topic 名称与数据流 |
| `HeightMap_` | stamp、frame_id、resolution、width/height、origin、data | 有消息类型，topic 和 data 编码/高度单位待确认 |
| `UwbState_` | 距离/角度估计、tag/base 姿态、遥控状态、错误和 enable 状态 | 有消息类型；对应 topic 未由当前 Go2 wrapper 明确绑定 |
| `Go2FrontVideoData_` | time_frame 与 720p/360p/180p 字节数组 | 有消息类型；DDS topic 未确认。优先通过 `VideoClient` 获取单帧样本 |
| `AudioData_` | time_frame 和音频字节数组 | 类型存在，但当前 SDK 示例的音频客户端属于其他机型；不能据此声明 Go2 支持音频采集 |

`RobotStateClient` 不是完整底盘状态查询客户端。它提供 `ServiceList()`、`ServiceSwitch(name, switch, status)` 和 `SetReportFreq(interval, duration)`。示例切换 `sport_mode` 服务，并调用 `SetReportFreq(3, 30)`，但参数单位/有效范围没有在头文件中解释；ServiceSwitch 会改变机器人服务状态，默认应限制为维护操作。

## 图像、跟踪、VUI 和配置服务

| 客户端 | SDK 方法 | 可封装用途 | 限制 |
|---|---|---|---|
| `VideoClient` | `GetImageSample(vector<uint8_t>&)` | 按需获取前置相机图像样本 | 官方示例将返回内容写成 `.jpg`；仍需验证格式、分辨率、帧时间戳和采集隐私策略。不要将原始图像塞入普通 observation |
| `UtrackClient` | `SwitchSet(enable)`、`SwitchGet(enable)`、`IsTracking(enable)` | 切换/查询 UWB tracking 开关和状态 | 服务名为 `uwbswitch`；状态只给 bool，不包含目标位置/身份，不能作为人员跟踪能力声明 |
| `VuiClient` | `Set/GetSwitch`、`Set/GetVolume`、`Set/GetBrightness` | 语音交互开关、音量和屏幕亮度设置/查询 | 设置类操作只应由设备管理员/本地策略发起；具体值域以实机/API 版本验证为准 |
| `ConfigClient` | `Set`、`Get`、`Del`、`Meta`、`SubscribeChangeStatus` | 查询和维护机器人配置项、监听变化 | 可写入或删除机身配置；不暴露为 Agent 技能，键名白名单、备份和回滚由受信任维护工具负责 |

## 低级电机控制

`go2_pub.h` 封装 `rt/lowcmd` publisher；Go2 `LowCmd_` 可携带 20 路电机命令。`example/go2/go2_low_level.cpp` 展示 2 ms 周期发布、设置电机模式、q/dq/kp/kd/tau 和 CRC。`go2.h::shutdown()` 注释要求在发布 `rt/lowcmd` 前释放默认控制模式，代码通过 `MotionSwitcherClient` 检查并释放 motion mode。

这条路径会接管底层执行器，失控影响远高于高层运动服务。第一版 bridge 不应实现或暴露低级 publisher。只有在独立的本地可信控制器中，完成控制权、实时调度、消息完整性、过期命令处理、急停和断连行为的设计及硬件在环验证后，才考虑支持。

## C++ bridge 边界建议

当前实现提供 `bridge/` 下的 C ABI bridge：DDS domain 固定为 `0`，必须显式传入网络接口；进程内只允许一个 bridge handle，以匹配 SDK 的全局 DDS factory；初始化 `SportClient` 并拒绝客户端/服务端 API version 不一致；订阅 `rt/sportmodestate`，只在状态两秒内新鲜时报告健康并输出运动模式、速度、位置、SDK 时间戳和错误码。`go2_bridge_call` 调用除 `Damp` 以外的 SportClient 动作，并在 bridge 内拒绝超出 SDK 公布范围的 Move、Euler 和 SpeedLevel 参数。速度指令的持续时间由 Python driver 控制，结束或取消时调用 `StopMove`。低级电机命令和避障位移没有接入。bridge 仍需在目标机网络和固件上确认实机行为。

Go2 C++ bridge 应只做厂商 SDK 相关工作：初始化 DDS/client、订阅消息、调用选定的高层客户端、转换单位/坐标系、生成明确成功/失败结果。Python 的 `UnitreeGo2Driver` 继续负责映射到 `CapabilitySpec`、`Observation`、`Command` 和 `Feedback`；不要让客户端方法列表自动变成可发现技能。

每条运动命令至少要有本地参数白名单、最大有效期、重试策略、设备状态前置条件和停止动作。SDK `int32_t` 返回值只表示 API 调用结果，不能单独作为 physical action 的 `succeeded`；完成状态需由后续 `SportModeState_`/`LowState_` 反馈和明确条件证明。断连或反馈过期时，应停止继续发送运动命令并报告 failed/unknown，停止策略必须在本地运行。

不要把未验证的速度、距离、角度、关节限位、BMS 单位或状态码写入 profile。候选统一观测可以包括运动状态、IMU 摘要、电池摘要、电机健康摘要、手柄状态和短期图像资源引用；数据需带采集/接收时间、单位、坐标系和来源。

## 实机验证前的核对清单

- 锁定 SDK revision、目标架构、Go2 型号、固件版本和各服务端 API version；客户端/服务端版本不匹配时明确拒绝运动功能。
- 确认 network interface、DDS domain、设备身份和高层控制权/lease；测试遥控器、官方 App 与 API 同时控制时的行为。
- 对每个高层动作记录单位、范围、前置/结束状态、超时、可中断性、重复命令与取消语义。
- 记录 `Move`、避障客户端和步态方法的控制源优先级、自动停止/避障具体行为；不要假设避障功能是安全认证或硬件防护。
- 验证 StopMove 是否能在动作中打断、反馈停止时间；另行确认实体急停和网络断开时的机身本地行为。
- 验证 DDS 消息频率、时间戳质量、丢包/CRC、字段单位/坐标系和关节索引；过期或异常反馈不参与成功判定。
- 按需验证视频格式与隐私边界；确认 UWB/LiDAR/点云/音频的 topic、设备选配和 Go2 固件支持情况。

以上实机核对仍未完成。development profile 已经声明包装后的 Sport 动作，但 API 返回 0 不能代替这些核对结果。
