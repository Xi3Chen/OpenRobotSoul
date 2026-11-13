# 级联语音助手：最低首字延迟 + 边说边推理动作（调研笔记）

> 对象：现有 OpenRobotSoul 级联管线（KWS→Silero VAD→流式 zipformer ASR→DeepSeek Flash 语言网关→技能→Xiao-Ya VITS→pacat）。
> 目标：闭集命令 TTFA≤300ms、开放请求 TTFA≤800ms（p50）、动作在 TTS 念到关键词前就绪、可打断。
> 标记：「实测」=本机/本仓已知；「待验证」=须在 Mi Max 2（A53×8/3.5GB/musl）上量测；外部数字均给 URL。

## 0. 五条关键结论
1. 延迟大头是“串行 + 整句 TTS”，不是模型：TTS RTF≈1.8 且合成完才播放，1.9s 音频要 ~3.3s 才出声。把它改成“短单元 + 预合成 PCM + 回调直写 pacat”，可省 1–2s。
2. 端点不必等“说完”：固定命令在 partial 稳定时就 dispatch 动作（speculative），端点只做确认。论文实测（Android 真机级联）中位 TTFA 5.79s→4.60s、p99 19.76s→15.10s，且推测仅限只读工具、LLM 原路径兜底，最坏不慢于串行（[arXiv:2610.07641](https://arxiv.org/abs/2610.07641)）。
3. 动作要分级：照明/查询/音量等可推测执行；移动、不可逆动作必须 final transcript 后才提交。当前“向前/向后”易混（见 asr-accuracy 任务），方向类动作绝不能吃推测结果。
4. TTFT 最便宜的开关：`stream=true`、关 thinking、稳定前缀命中磁盘缓存。当前 `robotsoul/language_gateway.py` 是非流式 `urllib` 请求、未关 thinking——这是最先改的（[流式 API](https://api-docs.deepseek.com/api/create-chat-completion)、[磁盘缓存](https://api-docs.deepseek.com/guides/kv_cache)）。
5. sherpa-onnx **没有逐帧流式 TTS**：源码只有 `offline-tts-*`，`OfflineTts.generate(text, cfg, cb)` 按“句”回调，粒度由 `max_num_sentences` 控制（[CMakeLists](https://github.com/k2-fsa/sherpa-onnx/blob/master/sherpa-onnx/csrc/CMakeLists.txt)、[offline-tts.h](https://github.com/k2-fsa/sherpa-onnx/blob/master/sherpa-onnx/csrc/offline-tts.h)）。所谓“流式”= 自己切短句 + 预合成缓存 + 回调里写 pacat。

## 1. 现状拆解（把 4–6s 摊开）
| 环节 | 当前事实 | 可压缩点 |
|---|---|---|
| 端点 | `robotsoul/voice/engine.py`：Silero `min_silence_duration=0.25`；ASR `rule1=0.5s`、`rule2=0.3s` 两条端点路径串联 | 双端点会叠加等待；命令模式可降到 0.10–0.15s + 词法守卫 |
| ASR final | 端点后等解码器 trailing 再 final（待验证拆分） | 端点前已可用的 partial 先做决策 |
| LLM | 非流式、`temperature=0`、无超时分级；无 thinking 关闭 | stream + thinking off + 缓存 + 短 max_tokens |
| TTS | RTF≈1.8、默认 `TTS_THREADS=1`、整句 `generate` 后才 pacat | 线程、短单元、预合成、回调直写 |
| 播放 | pacat 进程启动 + 整段写入 | 常驻进程 + 分块写 |

## 2. 端点/EOU：从固定静音到语义端点
- Silero 关键参数（sherpa-onnx C++ 默认值，Python 字段同名）：`threshold=0.5`、`min_silence_duration=0.5s`、`min_speech_duration=0.25s`、`window_size=512`（16kHz）、`max_speech_duration=20s`，退出阈值默认 `max(threshold-0.15, 0.01)`（[silero-vad-model-config.h](https://github.com/k2-fsa/sherpa-onnx/blob/master/sherpa-onnx/csrc/silero-vad-model-config.h)）。
- sherpa endpoint 规则是“或”的三条：`rule1(2.4s 无语音也结束)`、`rule2(解码到内容后再静音 1.2s)`、`rule3(整句 20s 强制结束)`，字段 `ruleN_min_trailing_silence / ruleN_min_utterance_length`（[endpoint.h](https://github.com/k2-fsa/sherpa-onnx/blob/master/sherpa-onnx/csrc/endpoint.h)）。命令模式建议 rule1≈0.6s、rule2≈0.2s、rule3 整句上限≈8s；对话模式 rule2≈0.5s。
- 压到 100–300ms 的代价有公开数据：LiveKit Turn Detector v1 在 300ms 延迟预算下误切 9.9%，600ms 下 4.5%；固定 5% 误切时平均 543ms，10% 时 295ms（[LiveKit blog](https://livekit.com/blog/solving-end-of-turn-detection)）。→ 命令场景不要只降静音阈值，必须加“命令已完成”守卫。
- 语义端点两类（都跑在 VAD 之后，不是替代 VAD）：
  - Pipecat Smart Turn v3.2：Whisper-Tiny 主干≈8M 参数、int8 仅 8MB、16kHz 单声道≤8s、23 语含中文；部分 CPU 10ms、多数云实例<100ms、Pipecat Cloud≈65ms（[README](https://github.com/pipecat-ai/smart-turn)）。
  - LiveKit Turn Detector v1：音频原生（不经转写）、融合声学+语义，v1-mini 为 CPU 版；配套开源 eot-bench 方法学（[blog](https://livekit.com/blog/solving-end-of-turn-detection)、[docs](https://docs.livekit.io/agents/logic/turns/)）。
  - 共同用法：VAD 判静音后，把“本轮整段音频”（≤8s，短则前补零）跑一次；期间若又有语音，对整轮（含新音频）重跑，不能只喂新片段。
- 固定命令场景的判断：命令轮 <2s，语义端点主要价值是“别在我停顿处抢答”，对“说完到执行”收益有限且要和 ASR/TTS 抢 A53。建议：命令轮用“词法完成判定 + 短 VAD”，开放对话轮再挂语义模型；误切率用离线回放集统计（目标≤2%）。
- 另一条经验：**partial 已经足够决定时就不要等端点**；端点只用于决定“何时 final + 提交不可逆动作”。

## 3. 流式 partial → 提前意图/动作推理
- partial 稳定性：尾部 1–2 字会改写，只有“命令动词 + 槽位值”都出现且连续两次 partial 一致（或槽位为闭集枚举）才触发。论文的触发策略可参考：至少 `N_min=6` 词开始，之后每 `ΔN=5` 词、或停顿>`τ_pause=300ms` 触发一次预测（[arXiv:2610.07641](https://arxiv.org/abs/2610.07641)）。
- 子句级切分：按标点与连词（中文：然后/再/还要/并且）切子句，逐子句独立匹配；论文用 12 个粗粒度 topic 做候选元组，命令机器人只需“动作白名单 + 设备枚举”。
- speculative 执行的安全边界（机器人版）：
  - 只推测执行**只读/幂等/可撤销/无位移**动作（闪光灯、查询、音量、播报），并限制同一时刻 ≤1 个在途推测；
  - 结果带 `generation + TTL`，final 不匹配则丢弃/回滚（本仓已有 session/generation 机制可复用）；取消接口要幂等；
  - 移动/不可逆动作（前进后退、夹爪）只走 final 路径，并叠加方向词二次确认；
  - LLM 原 tool-call 路径保留为 fallback → 最坏延迟 = 当前串行基线（论文同款设计）。
- 流式工具调用解析：OpenAI 兼容 SSE 中 `tool_calls[].function.arguments` 是增量字符串（DeepSeek 文档里该字段类型为 string，逐 delta 行为按兼容约定、待本机抓包确认）；写增量 JSON parser，必需字段一收齐就 dispatch，不必等整个 arguments。`strict` 模式（`/beta`，需 `additionalProperties:false` 等约束）能保证 schema、便于提前判定，但仍是自回归生成，收全前不能当完整对象；模型可能幻觉参数，必须校验（[Tool Calls](https://api-docs.deepseek.com/guides/tool_calls)）。
- 已发表系统：本论文的 speculation（read-only 推测、结果直接注入 prompt、verified fallback；cache-ready 47.7%、浪费调用仅 5 次、hard-negative 误读 9.1%、对抗样本 F1 90.2%）；StreamRAG 在用户说话中预测工具查询，工具延迟 −20%、QA 准确率 11.1%→34.2%（[arXiv:2510.02044](https://arxiv.org/abs/2510.02044)）。LiveKit/Pipecat/TEN 提供“流式+打断”但没有“动作提前提交”的原生抽象，这块要自研。端到端流式系统（字节 Seed LiveInterpret 2.0 同传、Seed-ASR）把“听/说（译）重叠”作为目标，但公开材料未提供工具/动作提前执行机制，需细读（[2507.17527](https://arxiv.org/abs/2507.17527)、[2407.04675](https://arxiv.org/abs/2407.04675)）；FunASR runtime 的 online-partial + offline-纠错 2pass 形态可作 ASR 侧参考。

## 4. 首字延迟优化清单
**LLM 侧**
- `stream=true`（SSE，终止于 `data: [DONE]`；需要用量就加 `stream_options.include_usage`）；`thinking:{"type":"disabled"}` 或 `reasoning_effort:"none"` 关思考（DeepSeek 默认开 thinking，会加推理 token 的 TTFT）。
- 磁盘上下文缓存默认开启，**前缀完全匹配才命中**；`prompt_cache_hit_tokens/miss_tokens` 可观测，缓存构建需数秒 → 服务启动/空闲时发一次固定前缀请求预热，system prompt 与工具 schema 必须字节级稳定（时间戳、随机 id 不能放前面）。
- 闭集命令用正则/关键词+槽位字典（<1ms）或本地小分类器，LLM 只负责措辞或开放请求；短应答 `max_tokens≤64`；HTTP keep-alive/HTTP2 复用连接（TLS 握手量级 50–200ms，[Coval](https://www.coval.ai/blog/how-to-measure-voice-ai-latency-the-complete-guide/)）。
- DeepSeek Flash 的 TTFT 官方未公布 → 待验证，先在本机对同一 prompt 测 p50/p95（流式首 delta）。
**TTS 侧**
- 预合成常驻：固定话术（"好的"、"正在打开手电筒"、"已关闭"）启动时用 `generate` 合成，存 s16le 单声道（采样率以模型 `SampleRate()` 为准，22.05kHz≈44.1KB/s，几十条仅数 MB）；命中即直写 pacat，TTFA≈播放启动时间。
- 首单元要短：首个 TTS 单元 ≤4–6 字（"好的，"），之后按标点切句；`max_num_sentences=1` 让 callback 每句触发，callback 内**立刻拷贝并写 pacat**（`samples` 指针回调后失效），不要等 `generate` 返回。
- 算力：`ROBOTSOUL_VOICE_TTS_THREADS` 默认 1 是明显浪费。RPi4 参考 RTF（1/2/3/4 线程）：vits-zh-hf-fanchen-C 4.31/2.45/1.85/1.60；matcha-icefall-zh-baker 0.89/0.54/0.43/0.39；aishell3 0.37/0.22/0.17/0.16（[RTF 表](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/rtf.html)）。先调线程，再评估换更快模型；注意与 ASR 抢核，须整机实测。
- 播放：pacat 常驻、`--raw --format=s16le --rate=… --channels=1`、分块写 stdin；首包前不做重采样/格式转换（预转好）；缓冲参数以本机 `pacat --help` 为准。
- 并行编排：LLM 首 token 一到就把“可播前缀”（如"好的，"）送去 TTS/pacat；工具未返回先播填充语，返回后接结果——这正是“先填语、再结果”的流水线。
- 许可前置：换 TTS 模型前先解决 Xiao-Ya 权重仅限非商业使用的问题（见 [16-voice-interaction](../robot-agent-plan/16-voice-interaction.md)），matcha/aishell3 等候选同样要核许可。

## 5. 目标流水线与时间预算
```text
mic → VAD/EOU ─┬→ ASR partial ─┬→ 命令匹配(<1ms) ─→ spec dispatch(白名单) ─→ 动作执行
               │               └→ 端点/final ─────→ commit 或 rollback（含方向二次确认）
               └→ (开放轮) final → LLM stream → 首 token → 短句 TTS → pacat 分块播放
启动期：预合成 PCM 缓存 + pacat/LLM 连接预热；播放与推理线程解耦
```
**A. 闭集命令（目标 p50）**
| 环节 | 目标 ms | 说明 |
|---|---:|---|
| 说话结束→partial 命中 | ≤50 | 命令尾字稳定即触发（往往在说完前） |
| 动作 dispatch→完成 | 50–200 | 本机技能（如闪光灯） |
| VAD 端点确认（并行） | 100–200 | `min_silence` 0.10–0.15 |
| 首音（预合成 filler） | 30–80 | pacat 常驻、写首块 |
| **TTFA（说完→首音）** | **≤300** | 与动作并行 |
**B. 开放请求（目标）**
| 阶段 | p50 | p95 | 依据 |
|---|---:|---:|---|
| 端点 | 250 | 400 | 当前 0.25+0.3 配置（待实测拆分） |
| final transcript | 150 | 300 | 待验证 |
| LLM TTFT | 300 | 700 | 待验证（stream+thinking off） |
| 首单元 TTS（≤6 字） | 200 | 450 | RTF 0.5–0.8，待验证 |
| 播放首包 | 50 | 80 | pacat（待标定） |
| **TTFA** | **≤800** | **≤1200** | 行业参考：Twilio 中位目标 1115ms（上限 1400）；Coval TTS 中位 245ms；Pipecat 实测拆解 200+125+336+359≈1020ms |
**“念到‘电’时动作已就绪”的时序（闭集示例）**：0ms 说完 → ~50ms partial 命中 → ~100ms dispatch → ~250ms 动作完成 → ~300ms 首音"好的，" → "正在打开…"念到"电"约 1.2–1.8s ⇒ 余量 ≥900ms。结论：只要动作 <1s 完成就必然满足；真正瓶颈是首音 300ms，而不是动作。
**概率化验收**：埋点后按动作类别统计 `P(动作就绪 | 已念到第 k 字)`（闭集目标 ≥0.95 @ 关键名词前）；填充语时长按该类动作 P95 选：闪光灯类（<200ms）用"好的"，网络/技能类（<1s）用"我这就…"，>1s 用"我查一下，稍等"。

## 6. 可观测指标与埋点
| 指标 | 定义（单调时钟） | 目标 | 埋点 |
|---|---|---:|---|
| TTFA | VAD 语音末端（补偿 `min_silence`）→ 扬声器首个可听采样 | 闭集≤300ms / 开放≤800ms(p50)、≤1200(p95) | 播前时间戳 + 回环标定一次声卡延迟（待验证） |
| ENDPOINT_LAT | 语音末端 → 端点判定 | 150–250ms | VAD 帧事件 + endpoint 回调 |
| ASR_FINAL | 端点 → final transcript | ≤200ms | ASR 回调 |
| LLM_TTFT | 请求发出 → 首个 content/tool delta | p50≤300 / p95≤700 | SSE 首包 |
| TTS_TTFB | 首单元文本 → 首个 PCM callback | ≤200ms | TTS callback |
| ACTION_READY | dispatch → 技能 ack | ≤200ms | 技能返回 |
| SPEC_HIT/WASTE | 推测命中率 / 浪费率 | 命中≥70%、浪费≤5% | cache 表计数 |
| SPEC_FIRE_POS | 首个推测触发位置（已说字数/词数占比，论文 fire position≈50% 词） | 越低越好，须早于关键名词 | partial 日志 |
| CUT_RATE | 端点误切率 | ≤2%（命令集） | 离线回放集 |
| INTERRUPT_LAT | 打断语音起点 → 播放停止 | ≤150ms（需 AEC，见 barge-in 任务） | VAD + pacat kill 时间 |
- 每帧带 monotonic ts；结构化日志加 `turn_id / stage / duration`，按场景报 p50/p95/max；命名可照 Pipecat `UserBotLatencyObserver` 的 TTFB 分解与 OTel span（[docs](https://docs.pipecat.ai/api-reference/server/utilities/observers/user-bot-latency-observer.md)）。本仓 `Voice` 运行日志（17-runtime-log）加 stage 字段即可复用。
- 现状限制：服务是半双工（播报后清麦克风缓冲），本笔记的 TTFA 目标不依赖打断；真正“随时打断”需 AEC（见 barge-in 任务），否则扬声器回采会误触发。

## 7. 框架/参考实现：搬什么、不搬什么
| 框架 | 可直接借鉴 | 移植成本 |
|---|---|---|
| Pipecat（Python 帧管线） | Smart Turn v3.2 ONNX（8MB int8、含中文、10–65ms 级）、TTFB 埋点/延迟分解思路 | 整框架接入需重写音频/WebRTC 层 → 不建议；只搬模型+指标 |
| LiveKit Agents | Turn Detector v1/v1-mini（音频原生、CPU 版）+ eot-bench 方法学 | v1-mini 权重非 Apache（Model License），ARM 延迟待验证；WebRTC/云依赖不搬 |
| TEN framework | 扩展化多语言实时框架，TEN VAD / TEN Turn Detection，chained 与 realtime 示例（[README](https://github.com/TEN-framework/ten-framework)） | 等于重写管线，适合长期重构，不适合渐进改造 |
| Vocode | `conversation_speed`、`interrupt_sensitivity`、端点参数语义（[docs](https://docs.vocode.dev/open-source/conversation-mechanics)） | 只参考参数设计，不引入 |
| FunASR runtime 2pass | online partial + offline 纠错 + VAD + PUNC 的部署形态（[doc](https://github.com/modelscope/FunASR/blob/516c4f77/runtime/docs/SDK_advanced_guide_online.md)） | 换 ASR 栈时才用；当前 sherpa 栈不动 |
**结论**：阶段 0–2 全部自研即可（正则/缓存/流式/预合成），只“搬模型与指标”，不搬框架。**可直接搬进当前 Python 服务**：Smart Turn ONNX（可选）、预合成 PCM+分块 pacat、SSE/增量 JSON 解析、filler 策略、阶段埋点；**需较大改造**：语义端点常驻、AEC+全双工打断、更换 TTS 引擎（含许可）。

## 8. 分阶段落地（不改动现有管线主干）
- **P0（0.5–1 天，低风险）**：加阶段埋点；`language_gateway` 改 `stream=true`+关 thinking+稳定前缀；`ROBOTSOUL_VOICE_TTS_THREADS` 1→3；pacat 常驻+分块写。验收：拿到 TTFT/RTF/TTFA p50/p95 基线。
- **P1（2–4 天）**：闭集命令快路径：partial 正则+槽位匹配、filler PCM 预合成、首单元 ≤6 字、照明类动作提前 dispatch、模板化回复（不经 LLM）。验收：TTFA p50≤300ms；动作早于"电"；误触发≤1%。
- **P2（1–2 周）**：开放请求：LLM 流式 + 增量 JSON tool dispatch + speculative/commit/rollback + 指标看板。验收：TTFA p50≤800ms、p95≤1200ms、浪费≤5%。
- **P3（2–4 周）**：语义端点（Smart Turn/LiveKit mini 在 A53 上评测）、AEC+barge-in（依赖 barge-in 任务）、长跑与温度。每阶段 feature flag + 一键回退到当前非流式路径。

## 9. 待验证清单
DeepSeek Flash 在本机网络的 TTFT；A53 上 TTS 线程 1–4 的 RTF/温度与换 matcha 的音质/许可；Smart Turn int8 在 A53 的单次推理与内存；pacat 首包与声卡可听延迟标定；`min_silence` 0.10–0.15 在真实噪声下的误切率；LiveKit v1-mini 权重许可与 ARM 可行性；`strict` 模式对首 token 时间的影响。
