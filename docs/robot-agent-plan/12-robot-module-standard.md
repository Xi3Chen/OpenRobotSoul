# 机器人模块组织规范

## 目录和所有权

所有机器人相关代码都按机器人代号放在仓库根目录的 `adapters/<robot_code>/` 下。一个模块拥有该机器人唯一相关的 profile、连接/生命周期、感知映射、动作能力、厂商 SDK bridge、测试夹具和第三方依赖。

```text
adapters/
  <robot_code>/
    __init__.py
    module.json
    adapter.py
    factory.py
    driver.py
    profiles/
      <profile_id>.json
    third_party/
      README.md
      sdk/
```

模块可按复杂度增加 `sensors/`、`actions/`、`tests/` 等子目录，但厂商协议代码不得放入 `robotsoul/runtime.py`、公共数据模型或其他机器人的模块中。`third_party/` 只放此机器人适配器所需的 SDK、驱动或其 pinned source checkout；跨机器人共用的项目级工具仍放项目根级 `third_party/`。

## 运行目标选择

部署选择放在 `config/robot.json`：

```json
{
  "robot_module": "unitree_go2"
}
```

仓库当前配置选择 `unitree_go2`，表示当前开发目标为 Unitree Go2。SDK bridge 未就绪时启动失败属于预期，不得静默回退到 simulator；本地模拟运行显式传入 `--robot-module simulator`。

`robot_module` 是必填的静态注册 ID；`profile_id` 可选，缺省时使用该模块 `module.json` 中的 `default_profile`。不得在配置中放 Python import path、动态库路径、可执行代码或命令。CLI 的 `--robot-module` / `--profile` 只覆盖本次运行，不会修改部署配置。

模块至少维护一个 `module.json`：

```json
{
  "robot_module": "unitree_go2",
  "default_profile": "unitree-go2-development"
}
```

核心 `robotsoul.adapters.registry` 是唯一允许修改的静态模块注册表。新增机器人时，在这里显式注册 factory，并在 `robotsoul.profiles.MODULE_DIRECTORIES` 登记模块目录。配置只选择已登记模块，不触发任意动态 import。新增同一机器人不同固件/部署 profile 时，只添加该模块的 profile 并调整 deployment config，无需改 Agent 或事务流。

## Profile 归属

- Profile 文件必须位于所属模块的 `profiles/` 目录，且 `profile_id`、`robot_module` 与文件路径/模块清单一致。
- Profile 只描述一个机器人实例、其能力、限制、设备和版本引用。
- Profile 声明的能力和参数限制是最高上限；运行时只将 profile 与设备发现、本地安全策略的约束交集暴露给 Agent。
- Profile 不包含厂商调用逻辑、任意代码、密钥或未经验证的安全限值。
- 设备身份、固件/协议兼容性或必需能力不匹配时，适配器初始化必须失败。

## 新增机器人流程

1. 在 `adapters/<robot_code>/` 创建模块骨架和 `third_party/README.md`，记录 SDK 来源、固定版本、许可证、支持固件、连接/控制权、反馈、停止和断连行为。
2. 在模块内实现 SDK bridge 和规范 `RobotAdapter`，不把 vendor API 泄漏到核心 runtime。
3. 新增零动作或仅已验证低风险动作的 profile；任何运动能力必须带实测单位、范围、超时、可中断性、完成证据与失败/停止语义。
4. 更新静态 profile 目录和 adapter factory registry，并设置模块默认 profile。
5. 编写契约测试，覆盖身份错配、能力交集、参数拒绝、反馈生命周期、超时、取消、断连和重复命令。
6. 在硬件安全条件下完成硬件在环验证；profile 在完成验证前标记为 development/unverified，不启用物理动作。

切换机器人型号时改 `config/robot.json` 中的 `robot_module`，或通过 `profile_id` 选择同模块的另一个已验证 profile。不能通过扩大 core runtime 中的品牌分支来接入新硬件。
