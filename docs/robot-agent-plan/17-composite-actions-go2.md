# Go2 组合动作封装候选清单

本文列出 Unitree Go2 适合封装为完整语义技能的组合动作。这些动作需要多次 SDK 调用、传感器闭环反馈或时序控制才能精确完成，直接暴露底层 `Move` 或 `Euler` 给 Agent 会导致参数选择困难和累积误差。

## 封装原则

1. **语义明确**：动作名称对应用户意图，AI 不需要计算具体参数。例如「左转」而不是「Move(0, 0, 某个 vyaw 值持续某个时长)」。
2. **传感器闭环**：需要 IMU、里程计或其他传感器反馈来判断完成，单次 SDK 调用无法保证精度。
3. **时序控制**：需要多步执行、等待、恢复中立位置或正弦/周期运动。
4. **幂等或可重试**：封装后的动作应明确是否 `retry_safe`；不幂等的动作（如前进1米）需要记录已完成的部分。

## 一、转向类（需要 IMU 闭环）

### 1.1 左转 90 度 `motion.turn_left`

**原始调用**：`Move(0, 0, vyaw)` 循环若干次

**传感器依赖**：`SportModeState_.IMU.rpy[2]`（yaw 角，弧度）或四元数换算的航向角

**实现逻辑**：
1. 记录起始 yaw 角 `yaw_start`
2. 循环调用 `Move(0, 0, vyaw_positive)`，每次约 1.5–2 秒
3. 每次调用后读取当前 yaw 角 `yaw_current`
4. 计算相对转角 `delta = normalize_angle(yaw_current - yaw_start)`（处理 ±π 边界）
5. 当 `delta >= π/2 - tolerance` 时停止，调用 `StopMove()`
6. 如果超过预算次数（例如 10 次）仍未到达，报告 `failed`

**参数**：无（固定 90 度）

**风险级别**：`confirm`（需要周围有足够转向空间）

**retry_safe**：否（已转过的角度无法撤销）

**预计时长**：3–6 秒

---

### 1.2 右转 90 度 `motion.turn_right`

**原始调用**：`Move(0, 0, -vyaw)` 循环若干次

**传感器依赖**：同左转

**实现逻辑**：与左转对称，目标 `delta <= -π/2 + tolerance`

**参数**：无

**风险级别**：`confirm`

**retry_safe**：否

**预计时长**：3–6 秒

---

### 1.3 向后转 180 度 `motion.turn_around`

**原始调用**：`Move(0, 0, vyaw)` 循环若干次

**传感器依赖**：同上

**实现逻辑**：目标 `|delta| >= π - tolerance`；方向任选顺时针或逆时针，取决于当前障碍分布（MVP 范围内可固定一个方向）

**参数**：无

**风险级别**：`confirm`

**retry_safe**：否

**预计时长**：5–10 秒

---

### 1.4 转向指定角度 `motion.turn` (保留为 `high` 级)

**原始调用**：`Move(0, 0, vyaw)` 循环

**传感器依赖**：IMU yaw

**实现逻辑**：通用版本，参数 `degrees` 可为任意值

**参数**：`{"degrees": [-360, 360]}`

**风险级别**：`high`（任意角度可能误用，只对 Dashboard 操作员开放）

**retry_safe**：否

**预计时长**：取决于角度

---

## 二、头部表情类（需要时序控制和 Euler 复位）

SDK 的 `Euler(roll, pitch, yaw)` 可以在 `balance_stand` 模式下调整机身姿态，但：
- 单位未在头文件明确，按示例推测为弧度
- 范围 roll/pitch ∈ [-0.75, 0.75]，yaw ∈ [-0.6, 0.6]
- **没有完成信号**，需要固定等待时长
- 调用后机身保持该姿态，需要主动恢复到中立位（0, 0, 0）

### 2.1 歪头 `expression.tilt_head`

**原始调用**：
1. `Euler(roll_target, 0, 0)`，等待 1 秒
2. `Euler(0, 0, 0)`，恢复中立

**传感器依赖**：无（纯时间控制）

**实现逻辑**：
- 左歪：`roll_target = 0.4`（约 23 度）
- 右歪：`roll_target = -0.4`
- 保持 1 秒后恢复

**参数**：`{"direction": "left" | "right"}`

**风险级别**：`normal`（幅度小，可逆）

**retry_safe**：是（重复歪头无副作用）

**预计时长**：2 秒

---

### 2.2 点头 `expression.nod`

**原始调用**：
1. `Euler(0, pitch_down, 0)`，等待 0.5 秒
2. `Euler(0, pitch_up, 0)`，等待 0.5 秒
3. 重复 `count` 次
4. `Euler(0, 0, 0)`，恢复中立

**传感器依赖**：无

**实现逻辑**：
- `pitch_down = 0.3`，`pitch_up = -0.3`
- 一次点头 = down → up，周期 1 秒
- 完成后恢复中立

**参数**：`{"count": [1, 5]}`（点头次数）

**风险级别**：`normal`

**retry_safe**：是

**预计时长**：`count` 秒 + 1 秒恢复

---

### 2.3 摇头 `expression.shake_head`

**原始调用**：
1. `Euler(0, 0, yaw_left)`，等待 0.5 秒
2. `Euler(0, 0, yaw_right)`，等待 0.5 秒
3. 重复 `count` 次
4. `Euler(0, 0, 0)`，恢复中立

**传感器依赖**：无

**实现逻辑**：
- `yaw_left = 0.4`，`yaw_right = -0.4`
- 一次摇头 = left → right，周期 1 秒

**参数**：`{"count": [1, 5]}`

**风险级别**：`normal`

**retry_safe**：是

**预计时长**：`count` 秒 + 1 秒恢复

---

## 三、平移与后退（需要里程计闭环）

SDK 已有 `sport.move_distance`，但它依赖融合位置和激光雷达里程计。当前实现：
- 前进：`vx > 0`，读取位置直到水平位移达到 `distance_m`
- 后退和侧移：需要验证 SDK 的坐标系和位置估计在负 vx / 非零 vy 下是否可靠

### 3.1 后退指定距离 `motion.back_up`

**原始调用**：`Move(-vx, 0, 0)` 循环，结合位置反馈

**传感器依赖**：`SportModeState_.position`（融合位置，米）和/或激光雷达里程计

**实现逻辑**：
1. 记录起始位置 `(x0, y0)`
2. 循环调用 `Move(-vx_safe, 0, 0)`
3. 每次读取当前位置 `(x, y)`，计算 `distance_traveled = sqrt((x - x0)^2 + (y - y0)^2)`
4. 当 `distance_traveled >= distance_target - tolerance` 时停止

**参数**：`{"distance_m": [0.2, 3.0]}`（后退距离限制在 3 米以内，超过需要多次调用或人工确认）

**风险级别**：`confirm`（后方盲区）

**retry_safe**：否

**预计时长**：根据距离，约 `distance_m / 0.5` 秒（假设安全后退速度 0.5 m/s）

**实机验证需求**：
- 确认 SDK 融合位置在后退时是否可靠（可能只在前进时校准）
- 确认后方障碍检测是否生效（Go2 有后向传感器吗？）

---

### 3.2 侧移 `motion.strafe_left` / `motion.strafe_right`

**原始调用**：`Move(0, vy, 0)` 循环，结合位置反馈

**传感器依赖**：位置估计

**实现逻辑**：与后退类似，但方向为 y 轴

**参数**：`{"distance_m": [0.2, 1.5]}`（侧移幅度更小）

**风险级别**：`confirm`

**retry_safe**：否

**预计时长**：根据距离

**实机验证需求**：
- Go2 侧移时步态是否稳定
- 位置估计在侧移时的精度

---

## 四、组合移动（多步串联，依赖前面的基础动作）

这些不是单独的封装函数，而是 Agent 通过规则调度多次调用基础动作完成。列在此处是为了在系统提示词中给出示例。

### 4.1 转身后前进

**Agent 规则**：
1. 调用 `motion.turn_around`，等待 `succeeded`
2. 调用 `sport.move_distance(vx=1.0, vy=0, vyaw=0, distance_m=2.0)`，等待 `succeeded`

**不封装原因**：转身和前进是独立技能，串联由 Agent 的规则调度完成；封装反而降低灵活性（如果只需要转身不前进呢？）

---

### 4.2 后退后转身

**Agent 规则**：
1. 调用 `motion.back_up(distance_m=0.5)`，等待 `succeeded`
2. 调用 `motion.turn_around`，等待 `succeeded`

**不封装原因**：同上

---

## 五、实现优先级与 MVP 范围

| 动作 | 优先级 | MVP 范围 | 依赖传感器 | 预计工作量 |
|---|---|---|---|---|
| `motion.turn_left` | P0 | 是 | IMU yaw | 1 天（含测试） |
| `motion.turn_right` | P0 | 是 | IMU yaw | 同上 |
| `motion.turn_around` | P0 | 是 | IMU yaw | 同上 |
| `expression.tilt_head` | P1 | 是 | 无 | 0.5 天 |
| `expression.nod` | P1 | 是 | 无 | 0.5 天 |
| `expression.shake_head` | P1 | 是 | 无 | 0.5 天 |
| `motion.back_up` | P2 | MVP 后 | 位置估计 | 1 天 + 实机验证 |
| `motion.strafe_left/right` | P2 | MVP 后 | 位置估计 | 1 天 + 实机验证 |
| `motion.turn` (任意角度) | P3 | 保留为 `high` 级 | IMU yaw | 复用转向逻辑 |

**MVP 范围（阶段 2A 首次端到端测试前完成）**：
- 三个转向动作（左转/右转/向后转）
- 三个头部表情（歪头/点头/摇头）

这六个动作足以让 Agent 在首次真实测试中展示「理解语义 → 选择动作 → 传感器闭环 → 完成反馈」的完整链路，同时风险可控（转向需要确认，表情无风险）。

**MVP 后**：
- 后退和侧移需要实机验证位置估计在非前进方向的可靠性
- 可能需要额外的后方/侧方障碍检测策略

---

## 六、状态注入与消息类型设计

根据最新设计，机器人状态不写入系统提示词，而是注入到每条消息前端。封装动作的实现需要从状态中读取传感器数据。

### 6.1 状态注入格式

```
this is robot status:
battery: 85%
motion_state: standing
imu_yaw: 1.234 rad
position: (1.2, 0.5) m
timestamp: 1731672345.678

this user say:
左转然后前进两米
```

或者心跳/定时任务触发：

```
this is robot status:
battery: 20%
motion_state: standing
position: (5.0, 3.0) m

this Heartbeat:
低电量，建议返回充电
```

### 6.2 封装动作需要的状态字段

| 动作类别 | 需要的状态字段 | 数据来源 |
|---|---|---|
| 转向类 | `imu_yaw: float` (弧度) 或 `imu_quaternion: [w, x, y, z]` | `SportModeState_.IMU.rpy[2]` 或四元数 |
| 位移类 | `position: (x, y)` (米)，`odom_distance: float` | `SportModeState_.position` 和激光雷达里程计 |
| 所有动作 | `motion_state: str`（standing/walking/lying），`battery: float` (百分比) | `SportModeState_.mode`，`LowState_.BMS.soc` |
| 表情类 | 无额外字段（纯时间控制） | — |

### 6.3 系统提示词中的消息类型说明

系统提示词应包含以下段落（在「可用技能」之前）：

```
## 消息格式

你会收到三种消息，每条消息都以「this is robot status:」开头，后跟当前状态快照，再跟消息来源和内容：

1. **用户消息**：
   this user say: {用户的话}

   这是用户直接对你说的，需要立即响应。

2. **心跳消息**：
   this Heartbeat: {系统提示，如低电量、传感器异常}

   这是定时检查触发的提示，不是用户说的。如果需要行动（如返回充电），主动提出；如果只是通知（如电量正常），无需回复。

3. **定时任务消息**：
   this cron task: {任务描述}

   这是预先设定的定时任务，按计划执行即可。

所有消息的状态快照都包含：
- battery: 电量百分比
- motion_state: 运动状态（standing/walking/lying）
- imu_yaw: 当前朝向（弧度，0 为初始方向，逆时针为正）
- position: 当前位置 (x, y) 米，相对起点
- timestamp: 状态更新时间（单调时钟）

**状态是实时快照，不要缓存或假设。** 每次决策前先看当前状态。
```

---

## 七、下一步工作

1. **实现转向三动作**（`motion.turn_left/right/around`）：
   - 在 `adapters/unitree_go2/actions.py` 添加函数
   - 在 `profiles/unitree-go2-development.json` 登记技能
   - 在 bridge 添加 IMU yaw 读取（如果 `SportModeState_` 还没暴露）
   - 写单元测试（模拟器下 mock IMU 反馈）

2. **实现表情三动作**（`expression.tilt_head/nod/shake_head`）：
   - 复用 `Euler` 调用，增加时序控制
   - 登记为 `normal` 级、`retry_safe: true`

3. **更新系统提示词模板**：
   - 消息格式说明
   - 技能列表动态拼接（从 profile 读取）
   - 组合动作示例（「左转后前进」= 两次调用）

4. **修改 `openrobotsoul-dialogue` 的消息包装层**：
   - 从 Python 运行时获取当前状态
   - 拼接成「this is robot status: ... \n this user say: ...」格式
   - 传给对话进程

5. **阶段 2A 首次真实端到端测试**：
   - 用户说「左转」，Agent 选择 `motion.turn_left`，执行并确认完成
   - 用户说「点个头」，Agent 选择 `expression.nod(count=1)`，执行
   - 记录分段延迟和 JSON 稳定性
