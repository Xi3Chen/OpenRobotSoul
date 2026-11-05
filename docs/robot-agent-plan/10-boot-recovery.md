# 启动监督与自恢复

## 目标

当 OpenRobotSoul 主程序因代码、配置或插件变更而无法启动时，机器人仍能进入安全状态，使用已知良好版本恢复服务，并在条件允许时让云端高能力模型协助诊断。云端 LLM 不是安全恢复的依赖。

## 独立启动监督器

Supervisor/Boot Watchdog 应独立于 Agent、插件运行时和可变业务包。职责包括：

- 校验当前 release manifest、签名/摘要、配置 schema 和依赖版本。
- 在启动主框架前将机器人控制器置于安全待机/停止状态；完成健康检查后才开放普通技能。
- 监督进程存活、健康探针、设备连接和心跳。
- 记录连续启动失败和故障版本，避免无限重启风暴。
- 原子切换当前 release 指针到上一个已知良好 release，并重启验证。
- 在必要时进入恢复模式；拒绝不兼容或未签名的更新。

Supervisor 只做进程与版本管理，不控制高层运动。独立硬件急停和底层控制器保护仍优先于 Supervisor。

## 发布槽与数据隔离

采用不可变发布目录或 A/B release slot：

- active：当前待运行版本。
- candidate：待验证的新版本。
- last_known_good：最近一次通过启动与健康检查的版本。
- recovery：最小恢复 UI/API、日志导出和版本切换能力。

代码/插件、profile/policy、地图与用户记忆、审计日志、密钥/凭证分开存放并配置独立权限。切换代码版本不能覆盖记忆、日志和凭证。Git 用于源码版本和隔离分支，不以盲目 git reset --hard 作为设备恢复策略。

版本激活应原子更新 manifest/指针；升级前保留恢复点。回滚只切换代码/配置 release，不重置用户数据。配置迁移必须可向前校验，并保留可恢复的旧数据格式或迁移备份。

## 启动与恢复状态

~~~mermaid
stateDiagram-v2
  [*] --> safe_boot
  safe_boot --> validate_release
  validate_release --> start_candidate: valid
  validate_release --> rollback: invalid
  start_candidate --> health_check
  health_check --> normal: healthy for window
  health_check --> rollback: failed / timeout
  rollback --> start_last_known_good
  start_last_known_good --> normal: healthy
  start_last_known_good --> recovery_mode: failed
  recovery_mode --> safe_wait: cloud unavailable
  recovery_mode --> diagnosis: cloud available and safe
  diagnosis --> repair_proposal
  repair_proposal --> isolated_validation
  isolated_validation --> approved_release: passed and authorized
  isolated_validation --> recovery_mode: failed / rejected
  approved_release --> validate_release
  safe_wait --> [*]
~~~

实际安全待机由机器人本地控制器/固件支持的方式定义。若无法可靠控制底盘，恢复模式不得发起探索或普通动作，并应报告需要现场处理。

## 恢复模式

进入恢复模式后：

- 本地控制器保持停止/安全姿态，关闭主动探索、问候移动和非必要动作。
- 允许读取健康状态、版本清单和脱敏日志；可提供显式的安全停止/恢复命令。
- 不自动重复安装同一失败 release，不允许修复代理写入 active 目录。
- 网络可用时，可把经过筛选的日志和 manifest 发给云端模型做诊断；无网络时仍能保持安全待机并提供本地日志。
- 当前版和 last-known-good 都启动失败时，停止自动尝试，进入人工恢复状态。

## LLM 辅助修复

云端高智力模型可解释错误日志、定位可能回归、生成代码或配置补丁和测试建议。修复过程在独立临时 worktree/container 和模拟设备中运行，不能访问生产密钥或直接连接真实驱动。

补丁需通过：

1. 变更范围和权限差异分类。
2. 静态检查、依赖与许可证检查、秘密扫描和构建。
3. 单元/契约测试及复现失败的启动测试。
4. 模拟器或录制回放测试。
5. L0/L1 插件适用自动发布策略；核心代码、唤醒策略、新权限或 L2/L3 变更要求人工批准。
6. 签名打包、candidate slot 启动、健康检查、canary 和自动回滚。

模型无法证明补丁安全时应保留恢复模式并请求人工处理。禁止模型直接运行任意 shell 命令、改写 Git 历史或修改急停、控制器、安全边界、密钥、权限和隐私规则。

## 失败策略

- 新版本启动失败：自动回滚 last-known-good，验证后恢复正常服务。
- 回滚版本也失败：停用主程序，进入最小恢复模式并请求人工介入。
- 云端不可用：执行本地回滚/安全待机，不等待 LLM。
- 重复崩溃：使用退避与失败计数；同一版本达到阈值后隔离，不循环重启。
- Git 仓库损坏或不可用：使用独立发布包/manifest 恢复，不依赖工作树状态。
- 数据迁移失败：停止使用新代码写入数据，保留原始数据并进入恢复流程。

## MVP 验收

- 候选版本启动失败后，Supervisor 自动回退到 last-known-good。
- 主框架不可用时，机器人进入本地定义的安全待机，主动行为保持禁用。
- 云端 API 断开不影响本地版本回滚和安全恢复。
- active 与 last-known-good 都失败时进入恢复模式，不无限重启。
- LLM 修复只能在隔离环境生成 proposal；未经对应风险等级授权不会发布。
- 回滚/修复不覆盖记忆、地图、审计日志或凭证。
- 修复版本经过启动健康检查失败时再次回滚，并保留可供诊断的日志和 release manifest。
