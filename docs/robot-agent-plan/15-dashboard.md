# 局域网 Dashboard 与接口风险门禁

## 范围与安全边界

Dashboard 是本地 RobotRuntime 的操作界面，不直接连接 SDK、DDS 或 ROS。浏览器只请求服务端注册的 typed operation；调用者身份和角色来自服务端会话，不能由 request body 提供。

风险等级与调用入口独立配置：

| 等级 | Agent | Dashboard | 门禁 |
|---|---|---|---|
| `normal` | 按 capability caller policy | 按 capability caller policy | 每次执行参数、机器人状态与适配器检查 |
| `confirm` | 可提出 | 已登录操作员确认后可执行 | Agent 必须经过无工具 AI 复核；没有 reviewer、复核拒绝或 digest 不一致时拒绝。未配置 reviewer 时，Dashboard 用本次操作员确认代替 AI 复核，参数绑定和适配器检查仍然生效。配置了 reviewer 时，Dashboard 同样先做 AI 复核 |
| `high` | 禁止 | 仅认证的 Dashboard operator | 本次操作明确确认、当前账户密码复核、绑定 command/task/args/policy 的确认标识和适配器再次校验 |

AI 复核与人员确认是不同记录和门禁。语音、文字任务或 UI 可见性不会自动授予权限。缺失/冲突策略、未知接口和过期状态默认不可调用。SDK 方法须逐项映射到固定 typed operation，不支持任意 SDK 方法透传。

`SwitchSet(false)` 这类关闭安全保护的操作默认维护专用或禁用。只有相应 SDK/固件行为、运行前置条件、停止与恢复流程经过验证后，才可讨论是否作为 high Dashboard 操作开放。

## Dashboard 页面

- 总览：robot_id、型号、连接/健康、活动任务、最近反馈和策略/profile 版本。
- 控制：Agent 自然语言任务及经过策略授权的 typed operation 表单。表单显示参数单位、范围和风险；不可用能力展示禁用原因。
- 能力目录：operation、风险、允许调用入口、参数 schema、超时、可中断性、策略来源和可用状态。
- 活动：任务与命令状态、AI 复核结论、操作员确认状态和适配器结果。
- 设置：大语言模型、音频和登录账户。模型设置只保存一条云端路径，字段为 provider、base_url、model、timeout 和 API key。音频设置写回 `config/voice.json` 的机器人名称、唤醒短语、语言、输入/输出设备和会话时间；模型 ID 仍只能使用静态 registry 中的值。登录账户替换原先的操作员 PIN。
- 语音目标设计：本地 PASSIVE 唤醒和 Dashboard WebRTC 通话共享 Voice Service；通话开始前须确认机器人本地麦克风已经关闭并释放，只有此后才接收浏览器音轨。登录或打开页面不自动请求麦克风权限。
- 总览页的「语音日志」预览展示最近的唤醒命中和最终识别文本，数据来自 `logs/voice_events.jsonl`。该预览和 `GET /api/voice/events` 都要求已登录的操作员会话。记录范围见[语音交互子系统](16-voice-interaction.md)。

## Dashboard API

- `GET /api/session`：当前会话状态。
- `POST /api/login`、`POST /api/logout`：操作员账户和密码登录/退出。请求体提供 `username` 与 `password`，不接受 PIN。
- `GET /api/settings`：已登录操作员读取模型、音频和账户设置。响应只包含账户名，不包含密码摘要或 API key。
- `POST /api/settings/llm`、`POST /api/settings/audio`：保存云端模型路径和音频配置。留空的 `api_key` 保持原值；显式空字符串会清除密钥。
- `POST /api/settings/accounts`、`POST /api/settings/accounts/delete`：新增或更新账户，以及删除其他账户。两次都要提交当前登录账户的密码。不能删除当前登录账户，也不能删除最后一个账户。
- `GET /api/dashboard/overview`、`GET /api/capabilities`：只读总览和能力目录。
- `POST /api/tasks`：提交自然语言任务，经 Agent capability surface 和 runtime 策略处理。
- `POST /api/actions/prepare`：服务端校验 caller、skill、schema、参数边界与当前策略，建立短时挑战；客户端不能设置 caller/risk/确认结果。
- `POST /api/actions/execute`：提交挑战 ID。服务端原子消费挑战，重验策略与 digest；高危操作还需再次输入当前账户密码，然后通过 `InvocationContext` 调用 Runtime。

挑战绑定认证 session、robot、task/trace、skill、规范化参数、风险与 callers、profile version、有效期。挑战仅使用一次；登出、过期、重放或策略变化都拒绝。失败不会调用 adapter。

## 认证和部署

初版服务使用标准库 HTTP server，不增加运行依赖。绑定 loopback 可用 HTTP 做本地开发；绑定明确的局域网 IP 必须提供 TLS certificate/key。拒绝 `0.0.0.0`、`::` 通配监听。Session cookie 为 HttpOnly/SameSite，写请求需要 CSRF token 并校验 Host/Origin；登录和高危操作的密码复核都有限速。

操作员账户写入 `config/operators.json`。账户名明文保存。密码不以明文或 MD5 保存，每条记录使用独立盐的 PBKDF2-HMAC-SHA256（200000 次）单向摘要，文件权限为仅当前用户可读写。首次启动如果文件里没有账户，从 `ROBOTSOUL_DASHBOARD_USER` 和 `ROBOTSOUL_DASHBOARD_PASSWORD` 创建；未设置时在终端询问用户名，并隐藏输入密码。文件里已经有账户时，启动过程不再读取这两个环境变量。密码和 API key 不写入日志。

云端模型配置写入 `config/llm.json`，权限同样限于当前用户。API key 需要原样提交给模型服务，因此文件中保留密钥本身，但 `GET /api/settings` 和页面只报告密钥是否已设置，不回显密钥。`config/operators.json` 和 `config/llm.json` 不纳入版本控制。

Dashboard LAN TLS 是控制操作的部署要求。浏览器麦克风 API 还要求 secure context；Dashboard 已接入认证后的 WebRTC 调用/应答/挂断 API，浏览器只在用户明确发起通话后申请麦克风。HTTP 只能用于 loopback 原型，不应控制真实机器人。当前代码和自动化测试不代表真实 Go2 的音频设备、ARM64 PyAV/WebRTC 与弱网行为已经验收。语音通话的音源互斥、通话断线期间本地麦克风保持关闭，以及挂断后的 PASSIVE/SLEEP 恢复行为见[语音交互子系统](16-voice-interaction.md)。

## 实施状态

当前 UI/API 主要用于 simulator。simulator 的移动技能标记 `confirm`。未配置 `SafetyReviewer` 时，Agent 不能执行确认级技能；已登录的 Dashboard 操作员确认后可以执行。Go2 profile 暴露 SportClient 动作。普通姿态、步态和限时移动是 `confirm`。翻滚、跳跃、倒立、舞蹈，以及摔倒恢复、手柄控制源和自动恢复开关是 `high`，只允许 Dashboard 操作员确认。`Damp` 没有暴露。所有公开 API 方法的策略清单仍需逐项审阅。

Dashboard 通话的第一方代码、单通话 WebRTC 媒体端点和浏览器控制已进入主仓库。通话租约由 Voice Service 音源仲裁器提供，本地音频设备必须先关闭并释放；模型/运行包下载和目标板安装见[部署指南](../deployment-guide.zh-CN.md)。浏览器端 `aiortc`/PyAV ARM64 wheel、麦克风/扬声器路由、实机低延迟及 10 小时稳定性仍未验证。
