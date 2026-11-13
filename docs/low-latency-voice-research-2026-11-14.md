# 低延迟语音对话调研报告（MiMax2 / OpenRobotSoul）

日期：2026-11-14
范围：本文回答四个问题——(1) 为什么本地命令识别差（“向后走”听成“向前走”）；(2) 为什么说完话到执行很久；(3) 如何“说话的同时推理动作”，让 TTS 念到“电”时动作已就绪；(4) 如何把首音延迟压到最低并做到随时可打断。

结论先行：

- **延迟大头不是 ASR/LLM，而是“串行 + 整句 TTS”**：TTS 在当前手机上 RTF≈2，整句合成完才开始播放；一轮“唤醒→回复首音”实测链路上限约 1.5–3 s（LLM 非流式）+ 1–5 s（TTS），而 ASR 在说完时已基本解码完毕（末字延迟≈0）。
- **识别问题的根因大概率在前端/端点/用 partial 抢跑，而不是小模型对命令不够**：同一批 10 条中文命令用 **edge-tts 神经音色生成、16 kHz 干净音频**在手机上实测，现有流式 zipformer 小模型 **10/10 全对**（包含“向前走/向后走”）；闭集 KWS 命令解码同样 **10/10**。仓库里的 2025-06-30 模型足以覆盖小命令集；需要补的是拾音前端、线程/重采样、置信度与闭集约束。
- **“念到电时动作已就绪”在闭集命令上成立**：实测 ASR 首个 partial 出现在 **0.5–0.8 s**（命令音长 1.2–1.8 s），闭集 KWS 命中点在 **0.8–1.5 s**（≈末字前后），解码本身只要 0.22–0.45 s；只要在 partial 命中时即派发（speculative），闪光灯这类动作在 **~1.2 s** 内完成，给“好的，我正在打开手电筒”留出 ≥900 ms 余量。
- **首音目标要分两类**：闭集/模板回复（可预合成）TTFA 可做到 **150–300 ms**；开放回复（LLM 自由文本）用当前 Xiao-Ya TTS 硬件下限约 **1.0–1.5 s**（“好”3 线程 0.98 s，“好的”1.37 s，加播放 ~0.1–0.4 s），要进 800 ms 必须换更快的 TTS（Matcha/Kokoro 类）或云 TTS。
- **可打断**在 MiMax2 只有一条现实路径：**级联 + PulseAudio/WebRTC AEC3 + 播放期常开 VAD + 快速停播/取消**，目标 <300 ms；端到端全双工模型（Moshi/Qwen2.5-Omni 等）最低也要 12–24 GB 显存级资源，3.5 GB 无 GPU 的 A53 不可行。

三份分项调研（含外部引用与可执行参数）见附录：
[ASR 准确率](research/asr-accuracy.md)、[低延迟对话](research/low-latency-dialog.md)、[打断与全双工](research/barge-in-full-duplex.md)。

---

## 1. 现状实测：一轮对话的时间花在哪

测量环境：MiMax2（Alpine/musl/aarch64，8×A53，3.5 GB RAM），sherpa-onnx v2.0.1 原生编译，Xiao-Ya VITS，DeepSeek Flash（`https://api.deepseek.com`，WiFi）。以下为 2026-11-14 手机直接实测（方法见 §7）：

| 环节 | 实测 | 说明 |
|---|---|---|
| 模型加载（一次性） | 47–54 s | 启动时；可用预热/常驻规避 |
| ASR 首个 partial | **0.50–0.80 s**（音频时间） | 命令音长 1.2–1.8 s，即说到一半已有可读文本 |
| ASR 末字/最终文本 | **≈说完瞬间**（-0.10~+0.04 s） | 流式解码 RTF 0.7–0.8（2 线程），不再有“等 final”的额外大头 |
| ASR 解码速度 | 0.82–1.36 s wall / 1.2–1.8 s 音频 | greedy、2 线程 |
| 闭集 KWS 命令命中 | **0.8–1.5 s**（音频时间），解码 wall 0.22–0.45 s | 10 条命令 10/10 |
| VAD 端点（Silero 0.25 s 静音） | 说话结束后 ~0.25–0.5 s | 可降到 0.10–0.15 s + 词法守卫，但有误切风险 |
| DeepSeek 非流式完整响应 | **1.42–1.48 s**（含 TLS/生成） | 当前语言网关是这种调用方式 |
| DeepSeek 流式首个 token（SSE） | **104–160 ms** | TCP 建连 27–199 ms、TLS 106–265 ms；TTFT 很低 |
| TTS 首句合成（2 句“好的，我正在打开手电筒”） | 1.49–1.60 s（3/2 线程）到首句回调 | callback 按“句”触发，非逐帧流式 |
| TTS 短句合成（3 线程） | “好”0.98 s、“好的”1.37 s、“我正在打开手电筒”3.13 s | 1.9–2.4 s 整句要 4.6–5.1 s（3/2 线程） |
| TTS 线程 | 1→2→3 线程 RTF：2.60→2.09→**1.91** | 3 线程最好；不是越多越好，需实测 |
| pacat 播放启动 | 冷启动（sink 唤醒）≈300 ms；热 ≈120 ms（0.3 s 音频 719→418 ms） | 保持 sink 唤醒/常驻播放器可省 ~300 ms |

由此得到当前端到端预算（用户说完 → 听到第一声）：

```
端点 0.25~0.5 s
+ ASR final ≈0（并行 partial 已可用）
+ DeepSeek 非流式 1.4~1.5 s
+ TTS 整句合成 4.6~6.4 s（2 句回复）
+ pacat 冷启动 ~0.3 s
≈ 6.5~8.7 s（还没算回复播报时长）
```

这解释了“说完话到执行之间花了很多时间”。**可压缩空间排序：TTS（最大）、LLM 非流式→流式、端点、播放启动、ASR 线程/重采样。**

## 2. 为什么识别差（以及“向后走→向前走”）

- 干净 16 kHz 神经音色命令实测：现有小模型 **10/10 全对**，包括“向前/向后/向左/向右”。所以小模型对 10–30 条命令集不是瓶颈。
- 真实链路里会掉点的位置：
  1. **拾音**：手机 Mic1 是远场/低信噪比，实测把电脑播报录回来 RMS 只有干净音频的 ~1/4（peak 0.2 vs 0.4+），且手机还会拾取自身扬声器；
  2. **用 partial 抢跑**：partial 尾部 1–2 字会改写，“向…”阶段就下结论必然出现“前/后”混淆；
  3. **端点切断**：VAD 静音 0.25 s 与 ASR 端点规则串行，短命令可能被提前截断；
  4. **采样率转换**：48 kHz→16 kHz 目前在 Python 里逐帧做，音质/CPU 都不理想；
  5. **解码配置**：代码默认 ASR 线程 1（手机 env 已设 2），greedy 无 beam，无热词/闭集约束。
- 提升路线（性价比排序，详见 [asr-accuracy.md](research/asr-accuracy.md)）：
  1. **P0 先把配置修对**（实测优先）：`ROBOTSOUL_VOICE_ASR_THREADS=2~3`；`decoding_method="modified_beam_search"`（CPU 约 greedy 1.5–3×，需确认 RTF<1）；把 48k→16k 移到 PulseAudio `module-remap-source rate=16000` 或向量化。
  2. **P1 闭集命令解码（最高性价比）**：复用已加载的 3M KWS，把 10–30 条命令写成 `keywords.txt`（`phone+ppinyin`），即“只能解码这些词”的微型 ASR；实测 10/10、命中点 0.8–1.5 s、解码 0.22–0.45 s，+0 模型内存。
  3. **P2 两遍解码**：流式小模型负责端点/草稿；说完后用 SenseVoice int8（228 MB，A55 RTF 0.44/0.26）整句重解，估 +0.5–0.8 s；只在低置信/方向词时触发（否则会破 700 ms 目标）。
  4. **P3 前端**：麦克风增益定标（约 -20 dBFS）、AEC/降噪、VAD 参数按真实噪声重标。
  5. **不建议**：上游 manifest 的 xlarge int8（约 4.4×参数，A53 估算 RTF 3.5–4，流式不可用；同配方 CER 仅 1.33→1.22）。热词对“前/后”这种成对合法词无定向作用，只能靠闭集/更强声学模型/语义纠错。
  6. **可选**：LLM 语义纠错（把 n-best/候选和技能集合交给 DeepSeek 纠偏），代价 +0.1–0.3 s 网络延迟，适合开放命令。

## 3. “说话时同时推理动作”怎么做

核心是 **partial 驱动的 speculative execution（推测执行）**，而不是等 final：

- 触发：命令动词 + 槽位都出现在 partial，且连续两次 partial 一致（或槽位是闭集枚举）→ 立即生成 intent；端点/final 只用于确认与提交。
- 安全分级（机器人必须做）：
  - **可推测**：只读/幂等/无位移/可撤销（闪光灯、查询、音量、播报）；
  - **不可推测**：移动、关节动作、任何不可逆操作，必须 final 文本 + 方向二次确认才 commit；
  - 同一时刻最多一个在途推测；结果带 generation + TTL，final 不匹配则丢弃/回滚（仓库已有的 session/generation 机制可直接复用）。
- 实测可行性（“念到电时已就绪”）：
  - 命令音长 1.25–1.8 s；ASR 首个 partial **0.5–0.8 s**；闭集 KWS 命中 **0.8–1.5 s**；命中后 dispatch + 动作执行 <300 ms。
  - 回复首音若用预合成模板，≈0.15–0.3 s 后开始播“好的，我正在打开手电筒”；按中文语速 4–5 字/秒，念到“电”（第 7–9 字）约 **1.5–2.2 s**，此时动作早已完成（~1.2 s 内），余量 ≥900 ms。
- 开放请求（LLM 路径）也能提前：LLM 流式输出 tool_calls 的 JSON arguments 是增量出现的，解析到“技能名 + 必填参数完整”即可 dispatch，无需等整个响应；同时让 TTS 先播固定 filler（预合成），LLM 完成后再播结果。参考 Android 真机级联论文（[arXiv:2610.07641](https://arxiv.org/abs/2610.07641)）中位 TTFA 5.79→4.60 s 的结论，其安全前提正是“只推测只读工具 + LLM 原路径兜底”。
- 工程落点（现有代码可直接改）：
  - `robotsoul/voice/service.py`：在 `_handle_asr_result` 之外增加 partial 回调路径；新增 `SpeculativeDispatcher`（generation/TTL/取消）。
  - `robotsoul/language_gateway.py` 或技能层：加“闭集命令快路径”，命中即本地生成 intent，不经过 LLM。
  - 动作侧：现有 `flash.toggle` 已经是幂等只读类，可作为第一个 speculative 试点。

## 4. 首字/首句 TTS 延迟怎么压

关键事实（实测）：sherpa-onnx **没有逐帧流式 TTS**，`OfflineTts.generate(text, sid, speed, callback)` 的 callback 按“句”触发（`max_num_sentences` 控制粒度）；Xiao-Ya VITS 在 A53 上 3 线程 RTF 1.91，“好”0.98 s、“好的”1.37 s 才出首段音频。因此：

1. **P0（不改模型）**：
   - 固定话术（“好的”“收到”“正在打开手电筒…”“抱歉，没有听清”）启动时**预合成 PCM 常驻内存**，首音≈播放启动延迟（热 pacat ~120 ms，冷 ~300 ms）；
   - TTS 线程 1→3；文本按 ≤6 字切成首单元，先合成首单元立刻播，后续单元并行合成（回调直写 pacat，不等整句 generate 完）；
   - pacat 常驻进程 + 分块写 stdin，避免每句重启；保持音频 sink 唤醒（或常驻播放器）。
   - 目标：闭集/模板回复 **TTFA 150–300 ms**。
2. **P1（开放回复）**：当前硬件下开放回复首音下限 ≈1.0–1.5 s（首单元合成 + 播放）。要压到 800 ms 以内需要：
   - 换**非自回归/更快的 TTS**（Matcha-zh、Kokoro 等，同等 A53 上首单元有望 0.3–0.6 s；需实测音质与许可）；
   - 或**模板化回复**覆盖机器人命令（大多数场景可模板化）；
   - 或云 TTS（牺牲离线/隐私）。
3. **LLM 首 token**：实测 DeepSeek Flash 流式 TTFT **104–160 ms**，非常低；当前差距在“非流式调用等完整响应 1.4–1.5 s”。改 `stream=true` + 关 thinking + 固定 system/tool schema 前缀（命中磁盘前缀缓存）+ `max_tokens≤64`，可把 LLM 对首音的影响压到 ~0.1–0.3 s。
4. 目标值建议：闭集/模板 TTFA p50≤300 ms；开放 p50≤800 ms、p95≤1200 ms（换 TTS 后）；同时记录“动作就绪时间”作为第二指标。

## 5. 随时可打断（barge-in）

MiMax2 现实路径 = 级联 + AEC + 分层打断（详见 [barge-in-full-duplex.md](research/barge-in-full-duplex.md)）：

- **AEC3 可用**：Alpine edge aarch64 的 `pulseaudio 17.0-r9` 依赖 `libwebrtc-audio-processing-1` 并提供 `module-echo-cancel`；pmOS 已移除旧 PA fork。需在设备上验证模块加载（历史上有 webrtc 未编译导致失败）。PulseAudio 的 webrtc 实现写死 `set_stream_delay_ms(0)`，路径延迟大时收敛差；PipeWire 的 echo-cancel 会按块补偿，Alpine `pipewire-echo-cancel` 可做 fallback。
- **打断流水线**：播放期常开 10 ms 帧 VAD + 能量门控 + 关键词快路径 → 决策 30–50 ms → 停 TTS（关 sink-input/丢 chunk）<100 ms → 动作取消令牌 + 安全停止；语义确认 150–250 ms 不阻塞“已停止”。防误触发：backchannel 过滤、误打断续播、短 pre-roll。
- **目标**：打断响应 <300 ms（听到用户开口到停止播报）。
- **不做的事**：取消 ≠ 回滚；不可逆动作执行中只做安全停止；推测/prefill 只预热，动作仍须语义确认后 commit。
- **端到端全双工模型**（Moshi/Qwen2.5-Omni/Step-Audio 2/MiniCPM-o/GLM-4-Voice）在 3.5 GB/无 GPU 的 A53 上不可行（最低 12–24 GB GPU 级或高端 SoC），不建议本轮尝试。

## 6. 推荐目标架构与分阶段落地

目标管线（级联、双路径）：

```
麦克风 → AEC（可选）→ 16 kHz
  ├─ 常驻 KWS：唤醒词 + 命令闭集（10–30 条）→ 命中即 speculative intent（只读/幂等动作）
  └─ 流式 ASR：partial → 闭集快路径 / LLM 流式 function-call（开放请求）
        ↓ intent + generation/TTL
   SpeculativeExecutor → 动作（安全分级）→ 结果
        ↓ 同时
   TTS：预合成模板 filler（常驻 PCM）+ 首单元合成 → pacat 常驻分块播放（可被 VAD 打断）
```

分阶段（每阶段带 feature flag，可一键回退）：

| 阶段 | 内容 | 验收指标 | 预计 |
|---|---|---|---|
| P0 埋点 + 低风险提速 | 阶段计时埋点；language gateway `stream=true`+关 thinking+前缀缓存；TTS 线程=3；pacat 常驻/分块；ASR 线程=2~3 | 拿到 TTFT/TTS_RTF/ENDPOINT/TTFA 基线；无功能回归 | 0.5–1 天 |
| P1 闭集快路径 + 预合成 | 命令闭集 KWS 解码（复用 3M 模型）；filler/模板 PCM 预合成；首单元 ≤6 字；闪光灯类 speculative dispatch | 闭集 TTFA p50≤300 ms；动作早于 TTS“电”；误触发≤1% | 2–4 天 |
| P2 开放请求流式化 | LLM SSE + 增量 JSON tool dispatch；speculative/commit/rollback；模板+首单元流水线；评估 Matcha/Kokoro | 开放 TTFA p50≤800 ms（换 TTS 后）；浪费≤5% | 1–2 周 |
| P3 打断与长跑 | PulseAudio AEC3 验证（fallback PipeWire）；播放期 VAD+快速停播+取消令牌；噪声/温度/10 h 长跑 | 打断 <300 ms；误打断可续播；10 h 稳定 | 2–4 周 |

**风险与前提**：

- 推测执行只能限只读/幂等动作；移动/不可逆动作必须 final + 确认（“前/后”混淆在开放路径仍可能，需方向二次确认）。
- 开放回复 800 ms 目标依赖更换更快 TTS（需核对模型许可；Xiao-Ya 当前为非商业许可）。
- AEC3 在 PulseAudio 上的实际收敛/CPU 占用需设备实测；不行则考虑 PipeWire 或“播报期间暂停采集”的半双工退化。
- 当前 ASR/端点实测基于干净合成音频；真实麦克风/噪声下的准确率需按 10 命令×20 人×3 环境（≈600 条）建评测集再定论。

## 7. 测量方法与数据来源

- 手机实测（2026-11-14，`sherpa-onnx` 2.0.1 / 当前 repo 代码）：
  - ASR/KWS：10 条中文命令（打开/关闭手电筒、向前/向后/向左/向右、停下、抬头、低头、原地不动）用 edge-tts `zh-CN-XiaoxiaoNeural` 生成、转 16 kHz 单声道 PCM，直接喂 `OnlineRecognizer`/`KeywordSpotter`；
  - TTS：同一模型用 `OfflineTts.generate(..., callback)` 测首回调/整句；线程 1/2/3；
  - LLM：手机直连 `api.deepseek.com`，用 `socket`+`ssl` 手工发 HTTP 请求，测 TCP/TLS/首个 SSE `data:`/非流式完整响应；
  - 播放：`pacat` 写 0.3 s PCM 测冷/热启动。
- 外部资料与可执行参数见三份分项笔记（24/22/多URL引用）：[asr-accuracy.md](research/asr-accuracy.md)、[low-latency-dialog.md](research/low-latency-dialog.md)、[barge-in-full-duplex.md](research/barge-in-full-duplex.md)。
- 未验证项（需下一步实测）：真实噪声下端点误切率；AEC3 的 ERLE/CPU；Matcha/Kokoro 在 A53 的音质/RTF/许可；`modified_beam_search` 在 A53 的 RTF；DeepSeek 前缀缓存命中率。
