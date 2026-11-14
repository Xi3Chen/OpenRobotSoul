# 语音交互子系统

## 目标与边界

Voice Service 为单台机器人提供本地待机唤醒、连续会话识别和 Dashboard 通话入口。它是机器人侧受监督的独立服务；下列职责是服务内逻辑组件，不要求各自拆成进程。音频采集、唤醒和实时识别不依赖云端 Agent。通过置信度和轮次检查的最终转写是自然语言任务，与 Dashboard 文本框进入同一条运行时入口，由 `robotsoul.language_gateway` 对照已注册技能规划。中间识别结果和音频帧不提交。唤醒或通话本身不授予动作权限；最终文本仍须经 RobotRuntime、caller policy、能力 schema 和本地安全检查。

部署时在 `config/robot.json` 设置 `robot_name` 与 `soul.personality`（机器人灵魂）；语言进程会把它们拼进系统提示词。`config/voice.json` 只保存唤醒短语、ASR 语言和设备偏好；`wake_phrase` 可用英文逗号分隔多个唤醒词，留空时默认使用 `robot_name`。名称与唤醒词可以相同，但显示名不是识别器配置的隐式来源。`config/robot.json` 仍负责选择 `robot_module` / `profile_id`。模型和设备均通过静态 registry 选择，不允许配置路径动态加载可执行代码。运行时代码和默认配置也随 Python wheel 打包。

建议先以 sherpa-onnx 的中文关键词检测、流式 ASR 和 VAD 作为原型候选，再在最终机器人计算板、麦克风和扬声器上选定模型。完整 ASR 只在唤醒或通话期间消费音频；为了缩短唤醒延迟，可在 PASSIVE 时预加载模型和解码器，但不运行完整语音解码。没有音源时模型卸载或保持休眠，按部署的启动时间/内存预算决定，不运行静默推理。

## 当前实现状态

第一方 Voice Service、设备适配器、音源仲裁、模型 wrapper、Dashboard WebRTC 端点及资源管理脚本已在主仓库实现，并有硬件无关自动化测试。模型资源默认尚未下载；Go2 的 ARM64 音频设备、PyAV/WebRTC 兼容性、声学指标、热/内存表现和 10 小时长跑尚未验证。下文的性能表和分阶段验收仍是目标，不作为已达成的质量承诺。

## 子组件

| 组件 | 职责与契约 |
|---|---|
| Audio Device Adapter | 枚举、监听热插拔、打开/关闭本地麦克风并输出带单调时钟时间戳的固定格式 PCM 帧；报告设备 ID、采样率、通道数、丢帧和健康状态。对 Go2 机身音频支持未确认时，首个实现允许使用外接 USB/I2S 音频设备。 |
| Capture Arbiter | 音频输入的唯一所有者。签发带 `session_id` / generation 的 `PASSIVE` 或 `CALL` 独占租约；每次切换先停旧源、关闭句柄、清空旧队列，确认释放后才接纳新源。拒绝并发租约、过期帧和旧 generation 的回调。 |
| Audio Front End | 有界帧队列、重采样、增益和按硬件能力启用的降噪/AEC；提供有限长度的内存 pre-roll 环形缓冲。队列满时丢弃旧帧并计数，禁止无界积压。原始音频不写磁盘。 |
| Wake Detector | PASSIVE 下唯一持续运行的推理器，检测配置的中文唤醒短语，输出置信度、时间和音源 generation。唤醒只建立交互会话，不代表身份认证。 |
| Streaming ASR + VAD | 唤醒后消费 pre-roll 和小块实时 PCM，持续产出 partial/final transcript；VAD 负责语音活动及端点，不能单独判断讲话是否面向机器人。低置信度、多人重叠或端点不确定时请求重复/唤醒，不提交动作任务。 |
| Turn / Addressing Manager | 管理唤醒、用户轮次、机器人回复、跟进窗口、空闲超时、取消和回到被动监听。通过唤醒词、当前交互的轮次时序及可用的方向/阵列线索判断是否继续对机器人说话；不把单纯 VAD speech 当成面向机器人。 |
| Dashboard Call Gateway | 仅接受已认证操作员显式发起的通话；负责信令、WebRTC 媒体和断线/挂断收尾。通话输入的 browser microphone 音轨经此网关转成 Voice Service 输入。通话最终转写与本地语音最终转写、Dashboard 文本任务同为自然语言，交给 `robotsoul.language_gateway`。 |
| Runtime / Response Gateway | 只传递 `wake.detected`、最终 `speech.transcript`、`interaction.cancelled` 和语音状态等有界事件；音频帧、partial transcript 不放进通用 Observation 队列。语音回复按当前模式路由到浏览器通话或已验证的机器人扬声器。 |
| Health Supervisor | 监控 Voice Service、设备租约、帧新鲜度、识别延迟和资源。语音服务重启不得重启 RobotRuntime 或阻塞机器人安全控制；进程失效时关闭音源，回到安全休眠并有限退避重试。 |

## 音源独占与状态机

同一时刻只允许一个音源租约。`CALL` 优先于本地 PASSIVE，通话开始时必须停止并释放机器人侧物理麦克风，再接收浏览器通话音频。浏览器的麦克风授权和机器人本地音频租约是两件事：先在服务端取得 `CALL_PENDING` 租约并确认本地采集已停止，再请求/启用浏览器音轨；用户拒绝浏览器授权或媒体协商失败时释放租约并恢复本地设备探测。这样不会出现两个输入同时打开。

~~~mermaid
stateDiagram-v2
  [*] --> SLEEP
  SLEEP --> PASSIVE: local microphone ready
  SLEEP --> CALL_PENDING: authenticated call request
  PASSIVE --> CALL_PENDING: authenticated call request / preempt passive
  CALL_PENDING --> CALL: local device released and browser media attached
  CALL_PENDING --> PASSIVE: call setup failed / local mic reacquired
  CALL_PENDING --> SLEEP: call setup failed / no local mic
  CALL --> CALL_RECONNECTING: transient media loss
  CALL_RECONNECTING --> CALL: media restored within grace period
  CALL --> PASSIVE: hangup / timeout / local mic reacquired
  CALL --> SLEEP: hangup / timeout / no local mic
  CALL_RECONNECTING --> PASSIVE: grace expired / local mic reacquired
  CALL_RECONNECTING --> SLEEP: grace expired / no local mic
  PASSIVE --> SLEEP: local microphone removed or unhealthy
~~~

| 状态 | 活跃组件 | 输入与行为 |
|---|---|---|
| `SLEEP` | 设备热插拔通知或低频探测、健康检查 | 通话未连接且本地麦克风不存在/不可用；KWS、VAD、ASR 全部停止，不打开采集流。通话可从此状态直接开始。 |
| `PASSIVE` | 本地采集、轻量 KWS；可预热但不推理完整 ASR | 机器人本地麦克风是唯一输入。检测到有效唤醒词后，将有限 pre-roll 交给 ASR，创建短期会话。 |
| `CALL_PENDING` | 仲裁器、媒体协商 | 暂停本地 KWS/ASR/VAD，关闭物理设备句柄并清除缓存；只有收到关闭确认才接纳 browser 音频。释放失败则拒绝语音通话并报告状态，不降级成双路混音。 |
| `CALL` | WebRTC 收流、VAD、流式 ASR、通话轮次/回复路由 | 机器人物理麦克风保持关闭。通话断开进入有限重连期但保持独占租约，不能自动切回本地麦克风，以免在同一通话期间采入环境声。 |
| `CALL_RECONNECTING` | 媒体重连、超时监控 | 不接纳本地音频；恢复后回到 `CALL`，宽限期耗尽则关闭媒体会话、释放租约并重新探测本地麦克风。 |

通话结束时依序关闭 browser track / WebRTC 收流、丢弃通话队列、释放 `CALL` 租约、重新探测设备，最后进入 PASSIVE 或 SLEEP。切换 source 会使未完成的 partial transcript 失效并发送取消事件；已经提交给 RobotRuntime 的任务按其原有生命周期处理。每条帧和异步回调必须匹配当前 generation，避免旧通话数据混入本地会话。

本地麦克风断连或异常时立即停用识别并进入 SLEEP；设备恢复后再进入 PASSIVE。浏览器 tab 关闭、显式挂断或媒体断线超过宽限期都会清理会话。若采集句柄无法确认关闭、lease 状态不一致或服务重启，默认 fail closed：不开放第二音源，清理后由监督器重启仲裁状态机。

## 对话结束与说话对象

VAD 只回答“此刻有语音”，不回答“这句话是对机器人说的”。唤醒后采用有界轮次：以静音端点结束一轮，机器人回复后只在配置的跟进窗口内等待控制者续话；达到无跟进超时或会话硬上限就释放 ASR 会话并回到 PASSIVE。通话则以显式挂断、媒体失败或通话超时结束。

控制者转而与旁人交谈时，Addressing Manager 应优先依赖新的唤醒短语、轮次时序，以及在设备支持时的阵列方向/波束信息；低置信度时回到 PASSIVE，不能把背景谈话继续当作机器人指令。单通道普通麦克风无法可靠判断讲话对象，也不能仅凭语音内容证明说话人身份。因此若目标是无需重复唤醒且可靠地区分人机对话与人际对话，必须有合适的阵列/声学前端并用目标环境录音验证；硬件不支持时采用短跟进窗口、设置会话硬上限或要求再次唤醒，不能承诺 Siri 级的说话对象识别。没有验证过的阵列/说话对象检测时，机器人每次回复后的 follow-up window 到期就回到 PASSIVE，不因环境中仍有人声而延长完整 ASR 会话。

对 PASSIVE 产生的会话，最终转写需带来源、置信度、语言、起止时间、privacy class、interaction/session ID 和是否通过 addressing gate。默认只把通过门控的最终转写交给任务入口；不保存环境谈话原始音频或连续 partial 文本。Dashboard 通话由已认证操作员明确启动，UI 显示麦克风/通话状态并由浏览器请求授权；登录或打开页面本身不自动开麦。

## 低延迟设计和指标

低延迟的关键是本地分层推理、模型预热、有界音频块和并行化媒体链路，而不是让大模型参与每个音频帧。KWS、VAD、流式 ASR 放在机器人侧；云端 Agent 仅处理最终文本的语义规划，其延迟单独计量。建议从 100 ms PCM 分块开始，在硬件测试后调优；ASR 模型在 PASSIVE 预热以避免唤醒后加载模型的长尾延迟。

以下为首轮目标板验收目标，不是对未知硬件的性能承诺。按目标设备长跑后的 P95 统计，并另列 P99 和掉线/丢帧：

| 指标 | 初始目标 | 计时边界 |
|---|---:|---|
| 唤醒检测延迟 | P95 <= 250 ms | 唤醒短语最后一个采样帧至 `wake.detected` |
| 本地 ASR 首个 partial | P95 <= 300 ms | 首个有效用户语音帧至可读 partial |
| 语音端点到 final transcript | P95 <= 700 ms | VAD 判定语音结束至 final transcript |
| final transcript 到 Runtime 入队 | P95 <= 200 ms | Voice Service final 至 task/interactions API 接收 |
| 局域网 WebRTC 单向媒体延迟 | P95 <= 150 ms | browser capture 至机器人 Voice Service 收帧；不含 ASR |
| 资源与队列 | 10 小时无持续内存增长、无无界队列 | 同时统计 RSS、CPU、温度、帧丢失、ASR RTF 和重连数 |

计时必须分开报告采集/传输、唤醒、ASR、端点、Runtime 入队和 Agent API，不把网络模型响应或物理动作时间并入 ASR 指标。WebRTC 需测实际部署网络；超出局域网范围、使用 TURN 中继或弱网时需另立延迟预算。安全控制和底层 watchdog 不得等待这些语音延迟目标。

## 配置草案

语音部署配置是独立版本化文件，当前 schema version 为 1：

~~~json
{
  "schema_version": 1,
  "wake_phrase": "索拉",
  "language": "zh-CN",
  "models": {
    "kws": "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20",
    "vad": "silero-vad-int8"
  },
  "speech": {
    "asr": {"base_url": "http://192.168.99.74:9000/v1", "model": "qwen3-asr", "timeout_seconds": 30, "api_key_env": "ROBOTSOUL_ASR_API_KEY"},
    "tts": {"base_url": "", "model": "", "voice": "", "timeout_seconds": 30, "api_key_env": "ROBOTSOUL_TTS_API_KEY"}
  },
  "input_device": "usb-microphone",
  "output_device": "default",
  "follow_up_seconds": 8,
  "session_max_seconds": 120,
  "call_reconnect_grace_seconds": 15
}
~~~

唤醒和静音检测仍用本地 ONNX 模型。一句结束后，整段语音交给 `speech.asr` 的 OpenAI 兼容接口 `POST {base_url}/audio/transcriptions`；回复交给 `speech.tts` 的 `POST {base_url}/audio/speech`。`base_url` 可以是云端，也可以是本机部署的同一套接口。环境变量 `ROBOTSOUL_ASR_BASE_URL`、`ROBOTSOUL_ASR_MODEL`、`ROBOTSOUL_ASR_API_KEY`、`ROBOTSOUL_TTS_BASE_URL`、`ROBOTSOUL_TTS_MODEL`、`ROBOTSOUL_TTS_VOICE`、`ROBOTSOUL_TTS_API_KEY` 覆盖文件里的对应项。密钥不写入配置文件。ONNX 运行库保留给后续视觉识别。唤醒继续使用 3M 关键词模型。`input_device` 只允许 `default`、`usb-microphone` 和 `usb-speaker`。`output_device` 在这三项之外还可以是 `unitree-go2`，表示把回复送到 Go2 连接后登记的项目内虚拟出口；没有登记这个出口时语音服务启动失败。实际本机设备可由 `ROBOTSOUL_VOICE_INPUT_DEVICE` / `ROBOTSOUL_VOICE_OUTPUT_DEVICE` 覆盖。新的一句开始播放时会停掉还没播完的上一句，被替换的那次播报不会再清麦克风缓冲。Go2 喇叭使用 `OPENROBOTSOUL_GO2_WEBRTC_IP`（可选 `OPENROBOTSOUL_GO2_AES_KEY`）。官方 App 同时占用 WebRTC 时喇叭连接会失败，运动连接不受影响。`follow_up_seconds`、静音端点和 call grace 均须在目标环境调参。唤醒短语的声学门槛是 `0.1`，关键词加权是 `3.0`。这是为两个字的短语放宽后的值；模型默认门槛是 `0.25`。喊完唤醒词后如果 `follow_up_seconds` 内没有识别到指令，会话结束并重新监听唤醒词，不会把这次会话一直留到 `session_max_seconds`。识别、技能执行或播报还没结束时，这段倒计时不开始；它们都空了才开始，并写下「回复结束，等待后续指令」。新的一句会停掉还没播完的回复，丢掉还没开始的排队文本，并取消正在执行且可打断的技能。闭集说法（例如「站起来」「向前走两步」）在整句识别返回后直接提交已注册技能，不再等待语言决策。识别器会把唤醒后的音频攒成一句，一句最长 20 秒，尾部静音后才结束；空白识别会重新开始这段等待，不会把会话留到 `session_max_seconds`。配置项不能绕过控制策略、扩大技能或允许任意模型代码。

Dashboard 设置页的「机器人个性」修改 `config/robot.json` 中的名称和灵魂；「音频」修改唤醒短语、语言、输入/输出设备和三段会话时间，并写回 `config/voice.json`。名称、灵魂和会话时间会作用于当前运行中的语音/语言路径（语言进程每次请求会重读机器人配置）；唤醒短语、语言和设备在下次启动语音服务后生效。模型 ID 不能从设置页改成静态 registry 之外的值。

## 运行日志中的语音记录

唤醒命中，以及唤醒后、通话中的识别文本，写入当天的运行日志，模块名为 `Voice`。流式中间结果还在增长时记为「识别中：…」；网络识别没有中间结果，一句结束记为「识别：…」。闭集命中再记「闭集技能：…」，随后记「提交运行时：…」。运行时模块接着记录收到的文本、语言决策、选中的技能、安全复核、开始执行，以及执行完成或未执行的原因。语音侧在运行时返回后记录结论或失败原因。同一句里没有变化的文本不重复记录。不再写 `logs/voice_events.jsonl`，总览页也不再提供语音日志。记录格式、隐私边界，以及它和工具调用审计的分工见[运行日志](17-runtime-log.md)。原始音频、每个音频帧和技能参数原文不写入日志。中间结果只进日志，不提交运行时。

资源 manifest 记录每个下载项的版本、官方 URL、大小、SHA-256、架构和许可证来源。Xiao-Ya TTS 归档内的 `MODEL_CARD` 标明其 BZNSYP/Data-Baker 数据许可仅限非商业用途；商业部署必须先替换为具备适用商业许可的语音模型并重新锁定哈希，不能把 sherpa-onnx 的 Apache-2.0 许可视为覆盖该语音权重。

## 分阶段验收

1. 在目标板上用模拟音源验证 Capture Arbiter：PASSIVE -> CALL_PENDING 时本地采音确已停止并释放句柄；未确认释放、重复租约、旧 generation 音频和挂断并发均被拒绝。
2. 用固定 Mandarin 测试集评估精确唤醒、漏唤醒、误唤醒、识别准确率、多人交谈时休眠行为和唤醒/转写时延；分别覆盖机器人播报回声、噪声与阵列方向。
3. 验证浏览器授权拒绝、通话启动/挂断、tab 关闭、弱网重连、Dashboard session 过期、Voice Service 重启、麦克风热插拔和无麦克风/无通话 SLEEP。
4. 在目标机器人上进行至少 10 小时混合静音、唤醒、对话、通话和断连长跑；检查内存/线程/文件描述符/队列不增长，日志不含原始音频，语音故障不影响 RobotRuntime 和本地安全控制。
5. 通过上述门槛后再进行多日 7x24 试运行。10 小时通过是阶段性证据，不等于 7x24 已得到证明。

## 参考

- sherpa-onnx [关键词检测](https://k2-fsa.github.io/sherpa/onnx/kws/index.html)、[在线流式识别模型](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/index.html)、[VAD](https://k2-fsa.github.io/sherpa/onnx/vad/index.html)。模型候选须逐一确认权重许可、运行库许可、目标架构支持和实机性能。
- 浏览器麦克风的安全上下文与权限约束：[MDN `getUserMedia`](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)。
- 媒体传输基础：[W3C WebRTC Recommendation](https://www.w3.org/TR/webrtc/)。
- 仓库现有语音事件和隐私契约：[机器人适配器契约](02-adapter-contract.md)；当前 Go2 SDK 音频能力尚未确认：[Go2 SDK 接口盘点](../../adapters/unitree_go2/SDK_INTERFACES.md)。
