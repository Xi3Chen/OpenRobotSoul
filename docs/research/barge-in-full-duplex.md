# 打断（Barge-in）与全双工语音可行性调研

> task-3 · 2026-11-14 · 对象：MiMax2 = Xiaomi Mi Max 2 / postmarketOS(Alpine, musl) / aarch64 8×Cortex-A53 / ~3.5GB RAM(+5GB zram) / PulseAudio / 内置 Mic1 48kHz 单声道 / 内置扬声器·麦克风互拾音。
> 现状：OpenRobotSoul 半双工（唤醒→ASR→DeepSeek Flash→整句 TTS→播放；播完丢麦克风缓冲并重置 ASR/VAD，因而不可打断）。目标：随时可打断、低延迟、说话时就能预推理动作。
> 标注：**[待验证]** = 尚未在本机实测，属经验值/估算；所有引用见文末编号。

## 0. 结论（先看这里）
1. **MiMax2 上现实路线只有「级联管线 + AEC + 分层打断」**。端到端全双工模型最低部署规格是 10–28GB 级 GPU 或 ≥16GB 统一内存（§3），3.5GB、无 GPU 的 A53 手机不在可运行范围。
2. **AEC3 在现有软件栈里可用**：postmarketOS 已不再携带 PulseAudio fork（pmaports `temp/pulseaudio` 已移除）[9]；Alpine edge community `pulseaudio 17.0-r9`（aarch64）依赖 `libwebrtc-audio-processing-1.so.3` 并提供 `module-echo-cancel.so`[4]；PulseAudio 构建要求 `webrtc-audio-processing-1 >= 1.0`（即 AEC3 代码库）[3]。
3. **最可能的坑**：PulseAudio 的 AEC 实现把 `set_stream_delay_ms(0)` 写死[2]，手机音频路径延迟大时 AEC 收敛变差；PipeWire 的实现会按块补偿额外延迟[8]，必要时可切到 `pipewire-echo-cancel`[5]。
4. **<300ms 打断可做到**：AEC 输出上 10ms 帧 VAD + 能量门控 + 关键词快速路径，决策 30–50ms、停止下发 <100ms，扬声器静音 ≈ 决策 + 已缓冲音频（几十 ms 至 ~150ms）**[待验证]**；语义确认再花 150–250ms，但停止已经发生。
5. **第一阶段不要碰端到端模型**：先做 AEC 虚拟源 + 播放期持续监听 + 快速停止/duck + 动作取消令牌 + 埋点，用数据决定第二阶段。

## 1. 回声消除（AEC）
### 1.1 可用方案与包（Alpine / postmarketOS aarch64）

| 方案 | 包（Alpine edge community aarch64） | AEC 后端 | 关键参数 | 备注 |
|---|---|---|---|---|
| PulseAudio `module-echo-cancel` | `pulseaudio 17.0-r9`，提供 `so:module-echo-cancel.so`，依赖 `libwebrtc-audio-processing-1.so.3`[4] | `aec_method=webrtc`（AEC3）/ `speex` / `adrian` / `null`[1] | `aec_args="mobile=0 noise_suppression=1 voice_detection=1 ..."`；`save_aec=1` 导出 `/tmp/aec_*` 便于离线算 ERLE[1][2] | 采样率被固定到 ≤48k（48k 档）、块长 10ms；`mobile=true` 走 AECM，省 CPU 但质量低[2] |
| PipeWire `libpipewire-module-echo-cancel` | `pipewire 1.6.11` + `pipewire-echo-cancel`（描述即 "WebRTC-based echo canceller"），依赖 `pipewire-webrtc`[5] | `aec/libspa-aec-webrtc`[6] | `node.latency=1024/48000`（=21.3ms 示例）、`aec.args` 前缀 `webrtc.*`、`monitor.mode`[6][8] | 10ms 块；把 TTS 固定到专用 sink 时用 `monitor.mode` 更稳 |
| SpeexDSP | `libspeexdsp`（pulseaudio 直接依赖）[4] | `aec_method=speex` | 参数文档缺失（PulseAudio 文档标注 to be documented）[1] | 收敛/双讲弱、CPU 低，作为回退 |

- PulseAudio webrtc 后端实际支持参数（源码）：`analog_gain_control`、`digital_gain_control`、`high_pass_filter`、`mobile`、`noise_suppression`、`transient_noise_suppression`、`voice_detection`、`pre_amplifier(_gain)`、`post_amplifier(_gain)`、`agc_start_volume`[2]。
- 加载示例（先 `pactl list short sinks/sources` 查真实 master 名）：
  ```sh
  pactl load-module module-echo-cancel use_master_format=1 aec_method=webrtc \
    aec_args="mobile=0 noise_suppression=1 voice_detection=1 save_aec=1" \
    sink_name=ec_sink source_name=ec_src sink_master=<sink> source_master=<source>
  ```
- **必须先验证 webrtc 后端真的在**：pmOS issue #2103 的现场是 PulseAudio 只编了 speex，`module-echo-cancel` 用 webrtc 参数加载失败、整模块 init 失败（日志 `speex.c: Failed to parse submodule arguments`）[10]。加载后检查 `pactl list modules short` 有无 `module-echo-cancel`，并看 `pactl list sources` 是否出现 `ec_src`。

### 1.2 延迟、CPU、ERLE
- **延迟代价**：AEC3 本身 10ms 帧；PulseAudio 还会引入虚拟 sink/source 两级流缓冲，端到端新增延迟 **[待验证]**（预计几十 ms），用 `pactl list sources | grep -i latency` 读实际值，并注意这个延迟同时会拉长“停止播报”的尾部。
- **CPU 开销**：A53 8 核、无 NEON 加速库保证；AEC3 单声道 48k 预计占单核个位数到 ~20% **[待验证]**。`mobile=1` 可显著降 CPU，但双讲/回声抑制变差，需实测取舍。
- **ERLE**：`ERLE = 10·log10(E[y²]/E[ŷ²])`（y=麦克风、ŷ=AEC 输出）[11]。注意公开结论：ERLE 只适合安静单讲场景，且与主观 MOS 相关性很低（ICASSP 2021 AEC Challenge 实测 PCC 0.31）[11]，**不能拿 ERLE 当“能否同时识别”的唯一指标**。
- 公开的大规模手机外放 ERLE 横向数据很少；经验目标 **[待验证]**：外放 5–20cm、音量约 60% 时 AEC3 常能压 15–30dB，SpeexDSP 更低（约 10–20dB）。
- 建议本机验收口径：播报单讲段 ERLE ≥15–25dB；纯播报 10min 内 VAD 误触发 <1 次；近端说话检出率 >95%（详见 §5）。

### 1.3 “播报中同时识别”是否可行
- 结构上可行：AEC 输出就是可用的“干净 mic 流”，VAD/关键词/流式 ASR 都能直接接；难点是手机外放音量高、扬声器非线性、mic 与扬声器同侧近距离，残余回声会抬高 VAD 底噪。
- 硬约束：**参考信号必须是“真正送到扬声器的信号”**（虚拟 sink 的输出），AEC 之后再改音量/EQ 会破坏线性模型；TTS 音量应固定并限制在 ≤60–70%，避免削波。
- 判定：AEC3 + NS 在 MiMax2 上大概率够用（A53 算力不是瓶颈），但必须按 §5 实测后才算数。

## 2. 打断检测与处理流水线
### 2.1 状态机
```
IDLE ──wake──▶ LISTENING ──final ASR──▶ THINKING ──首块 TTS──▶ SPEAKING
SPEAKING ──(AEC 后能量↑ + 连续 2~3 帧 VAD)──▶ BARGE_CANDIDATE
BARGE_CANDIDATE ──关键词/≤250ms partial 出现实词──▶ CANCELLING ──flush+取消完成──▶ LISTENING(带 pre-roll)
BARGE_CANDIDATE ──静音/backchannel──▶ SPEAKING(从 chunk 边界续播)
任意状态 ──E-STOP/危险──▶ SAFE_STOP(旁路一切取消逻辑)
```

- **分层触发（防误触发）**：① 能量门控：AEC 后帧能量 > 近 300ms 回声残差底噪 + 6dB；② VAD（Silero/webrtcvad，10ms 帧）连续 2–3 帧；③ 关键词快速路径（“停/别动/stop”，KWS 或 ASR 首包高置信）**直接** CANCELLING；④ 语义确认（≤250ms 内 partial 有实词、非“嗯/啊/对”）才最终确认，否则续播。
- **参考实现参数**：LiveKit 默认 `min_duration=0.5s`、`min_words=0`、`false_interruption_timeout=2s`、`resume_false_interruption=true`，并有 adaptive 模式区分真打断与 backchannel[20][21]；500ms 对“随时可打断”太慢，我们用关键词/语义双确认把 min_duration 压到 150–250ms，同时沿用它的“误打断后恢复播报”思路。
- **TTS 立即停止 / duck**：应用侧保持分句/chunk 队列；触发后立即停止写入并关闭（或 cork）当前 sink-input，丢弃未播 chunk；若停不下来先用 `pactl set-sink-input-volume <idx>` 把音量瞬间压到 30% 再停（module-role-ducking 也可做应用级 duck，但进程内控制更快）[1]。
- **LLM / 动作取消与回滚**：请求级 cancel token + 流式 abort；动作按“可逆/不可逆”分级——可逆动作（转头、靠近、亮灯）可回滚；不可逆动作（抓取、移动、释放）只允许在执行前取消，执行中改为**安全停止**而不是回滚；每个动作带幂等键，pending 队列整体可取消；E-STOP 最高优先级且旁路所有逻辑。
- **“说话时就能推理动作”**：ASR partial 即做意图预分类 + LLM 预热/prefill（参考 LiveKit preemptive generation：final transcript 前就开始生成，文本变化则取消）[20][21]；但机器人动作只在语义确认后 commit。
- **时延埋点**（统一 `time.monotonic()`）：t0 用户首帧、t1 VAD 决策、t2 停止下发、t3 sink-input 关闭、t4 扬声器静音、t5 partial 首词、t6 新回复首音；目标 t4−t0 <300ms（p95），t5−t0 ≤400ms，t6−t0 ≤800ms（第二阶段）。
## 3. 全双工端到端语音模型：能力与端侧现实

| 模型 | 架构/规模 | 公开延迟 | 内存/算力 | 端侧后端 | MiMax2 |
|---|---|---|---|---|---|
| Moshi (Kyutai) | 双音频流 + Inner Monologue；7B 时序 Transformer + Mimi 12.5Hz codec[12][13] | 理论 160ms（80ms 帧+80ms 声学延迟）、L4 实测约 200ms[12][13] | PyTorch bf16 需 24GB GPU；int4 仅 MLX(Apple)[13] | Python / MLX / Rust(Candle)[13] | ❌ 权重+K V 已超 3.5GB |
| Qwen2.5-Omni | Thinker-Talker，7B/3B，流式文本+语音[14] | 无统一公开延迟；MNN 手机实测 SD8Gen1：7B 解码 8.35 tok/s、3B 15.84 tok/s[14] | MNN 峰值内存 7B 5.8GB / 3B 3.6GB；bf16 最小显存 31GB(7B)/18GB(3B, 15s 视频)[14] | MNN(C++)/vLLM/PyTorch[14] | ❌ 3B 内存已超预算，A53 远慢于 SD8Gen1 |
| Step-Audio 2 / mini | audio encoder + LLM + flow-matching detokenizer；mini 由 Qwen2.5-7B 与 Qwen2-Audio 初始化[15][16] | 无公开端侧延迟；vLLM GPU 服务实时[16] | 需 GPU 服务 | vLLM/PyTorch[16] | ❌ |
| MiniCPM-o 4.5 | 9B = 0.4B ViT + 0.3B Whisper + 8B Qwen3 + 0.3B 语音解码；Omni-Flow 1Hz 全双工、无需外部 VAD[17] | 无毫秒级公开数；llama.cpp-omni 全双工需 M4 Max ≥24GB 或 Nvidia ≥12GB[17][18] | GGUF 10GB / AWQ 11GB / PyTorch 19GB[17] | llama.cpp-omni(C++)/vLLM[17][18] | ❌ 差 3–7 倍内存 |
| GLM-4-Voice | GLM-4-9B + Whisper-VQ tokenizer + CosyVoice flow decoder，12.5Hz；10/20 token 起播[19] | 只给 token 数，无 ms 数据[19] | CUDA bf16/int4[19] | PyTorch[19] | ❌ |

- **算力账（估算 [待验证]）**：A53@~2GHz×8 有效 INT8 大致 5–10 GFLOPS 级；7B 每 token ~14 GFLOP，Moshi 需 12.5 次/s ≈175 GFLOPS，差 1–2 个数量级。即便 3B q4 塞进内存（~2GB 权重+音频编解码），也只能到个位数 tok/s，达不到实时对话。
- 结论：**端到端全双工在 MiMax2 上不可行**；可行的是“小模型级联 + AEC + 打断”。等待方向是 1–2B 全双工模型 + llama.cpp-omni/MNN 级优化，在 A53 上 RTF<1 再评估。

## 4. 方案对比与推荐路线

| 方案 | 打断延迟 | 识别质量 | MiMax2 可行性 | 实施成本 | 结论 |
|---|---|---|---|---|---|
| 半双工（现状） | 不支持 | 高 | 已上线 | 0 | 不满足需求 |
| 级联 + AEC + 分层打断 | 决策 30–50ms；停止 <100ms，静音 ~100–300ms[待验证] | 高（AEC 后） | ✅ | 中（1–4 周） | **推荐** |
| 端到端全双工 | 模型级 200ms 起，端侧更慢 | 中高 | ❌（内存/算力） | 极高（换硬件） | 暂不投入 |

**推荐分阶段**（成本为人日估算，[待验证]）：
- **P1（1–2 周，核心）**：建立 AEC 虚拟 source；语音服务在播放期持续监听（不再丢缓冲，改为保留 pre-roll）；实现状态机、快速停止/duck、动作取消令牌与安全停止；完成 §5 的 ERLE/CPU/延迟/误触发埋点。改动集中在音频路由与语音服务状态机，不动业务动作实现。
- **P2（1–2 周）**：流式 ASR partial + 分句 TTS（首音更快、停止粒度更细）+ 预分类/prefill 预生成；补齐“误打断后续播”与动作回滚语义。
- **P3（观察）**：仅当出现 1–2B 级全双工模型且 llama.cpp-omni/MNN 在 A53 上 RTF<1 时再立项。

## 5. 可直接验证的实验步骤
**A. AEC 能力（ERLE / CPU / 延迟）**
```sh
# 1) 确认后端可用并启用 dump
pactl load-module module-echo-cancel use_master_format=1 aec_method=webrtc \
  aec_args="mobile=0 noise_suppression=1 voice_detection=1 save_aec=1" \
  sink_name=ec_sink source_name=ec_src sink_master=<sink> source_master=<source>
pactl list modules short | grep echo; ls -l /tmp/aec_*
# 2) 播放 30s 单讲 TTS/白噪（固定音量），录 AEC 输出
parec --device=ec_src --format=s16le --rate=48000 --channels=1 > ec.raw
# 3) ERLE = 10*log10(mean(mic^2)/mean(ec^2))，只在无近端语音段统计；mic 用 /tmp/aec_* 的 near dump
# 4) CPU/延迟：top -H -p $(pidof pulseaudio) 或 ps -o %cpu -p <pid>；pactl list sources | grep -i latency
```
矩阵：`aec_method=webrtc/speex` × `mobile=0/1` × `noise_suppression=0/1` × 音量 50/70/90% × 距离 5/15/30cm；每格 30s，记录 ERLE、CPU、source latency、VAD 误触发。

**B. 打断延迟埋点**
- 在原始 mic 与 AEC 流上同时算能量；t0 取人声起点（人工标注/回放触发），t4 用第二个麦克风或录制 `monitor` 检测“扬声器真正静音”；各场景 20 次，报 p50/p95。
- 场景：正常打断、单字“停”、backchannel“嗯/对”、电视噪声；判据 t4−t0 <300ms（p95）、误触发 <1 次/10min、backchannel 不触发。

**C. 端侧算力校准（决定端到端是否永远排除）**
- 用 `llama-bench`/MNN 在本机跑 0.5B/1.5B q4，记录 tok/s 与内存峰值；若 <5 tok/s，端到端路线维持“禁止投入”。

## 6. 风险
- pmOS 镜像可能仍在旧分支/带旧 fork，AEC 实际不可用；先按 §5-A 第 1 步验证，失败再评估 PipeWire 或自行编译 `-Dwebrtc-aec=enabled`。
- 参考点错误：TTS 若绕过虚拟 sink、或 AEC 后再改音量/EQ，AEC 直接失效（残余回声导致自打断）。
- 手机扬声器高音量非线性：AEC 线性模型压不干净，需限幅/限音量并留能量余量。
- 真实音频路径延迟未知，PulseAudio `set_stream_delay_ms(0)` 使 AEC 收敛变慢；延迟过大时切 PipeWire 或加显式延迟估计。
- 误触发/漏触发权衡；纯关键词易误停，纯语义太慢，需双确认 + 误打断续播。
- 动作安全：取消 ≠ 回滚；不可逆动作必须可安全停止或禁止执行中打断。
- CPU/发热/续航：AEC+VAD+ASR 常开需实测；3.5GB 内存下避免额外常驻模型，防止 zram 抖动。

## 7. 参考

1. PulseAudio Modules（module-echo-cancel 参数、aec_method/save_aec）: https://www.freedesktop.org/wiki/Software/PulseAudio/Documentation/User/Modules/#module-echo-cancel
2. PulseAudio webrtc AEC 源码（AEC3、参数表、`set_stream_delay_ms(0)`）: https://gitlab.freedesktop.org/pulseaudio/pulseaudio/-/raw/master/src/modules/echo-cancel/webrtc.cc
3. PulseAudio meson.build（`webrtc-audio-processing-1 >= 1.0`）: https://gitlab.freedesktop.org/pulseaudio/pulseaudio/-/raw/master/meson.build
4. Alpine pulseaudio 17.0-r9（aarch64，依赖 webrtc/speex，提供 module-echo-cancel）: https://pkgs.alpinelinux.org/package/edge/community/aarch64/pulseaudio
5. Alpine pipewire-echo-cancel（WebRTC-based echo canceller）: https://pkgs.alpinelinux.org/package/edge/community/aarch64/pipewire-echo-cancel
6. PipeWire libpipewire-module-echo-cancel 文档: https://pipewire.pages.freedesktop.org/pipewire/devel/page_module_echo_cancel.html
7. PipeWire PulseAudio 兼容 module-echo-cancel 文档: https://pipewire.pages.freedesktop.org/pipewire/devel/page_pulse_module_echo_cancel.html
8. PipeWire AEC webrtc 实现（10ms 块、块延迟补偿）: https://gitlab.freedesktop.org/pipewire/pipewire/-/raw/master/spa/plugins/aec/aec-webrtc.cpp
9. postmarketOS pmaports MR!4480（PulseAudio fork 与 AEC 历史）: https://gitlab.nura.eco/nura/pmaports/-/merge_requests/4480
10. postmarketOS issue #2103（module-echo-cancel 初始化失败现场）: https://gitlab.nura.eco/nura/pmaports/-/issues/2103
11. ICASSP 2021 AEC Challenge（ERLE 定义、与 MOS 低相关 PCC 0.31）: https://arxiv.org/abs/2009.04972
12. Moshi 论文（160ms 理论/200ms 实践）: https://arxiv.org/abs/2410.00037
13. Moshi 仓库（7B/24GB GPU/MLX/Rust）: https://github.com/kyutai-labs/moshi
14. Qwen2.5-Omni 仓库（Thinker-Talker、MNN 手机实测与最小显存）: https://github.com/QwenLM/Qwen2.5-Omni
15. Step-Audio 2 技术报告: https://arxiv.org/abs/2507.16632
16. Step-Audio 2 仓库（mini、vLLM 部署）: https://github.com/stepfun-ai/Step-Audio2
17. MiniCPM-o 4.5 文档（9B 架构、全双工、内存要求）: https://github.com/OpenBMB/MiniCPM-o
18. MiniCPM-o 4.5 llama.cpp 部署: https://github.com/OpenSQZ/MiniCPM-V-CookBook/blob/main/deployment/llama.cpp/minicpm-o4_5_llamacpp.md
19. GLM-4-Voice 仓库（9B、12.5Hz、起播 token 数）: https://github.com/zai-org/GLM-4-Voice
20. LiveKit turn-taking tuning（interruption 参数默认值）: https://docs.livekit.io/agents/logic/turns/tuning.md
21. LiveKit 打断配置指南（preemptive generation / adaptive interruption）: https://livekit.com/blog/turn-detection-and-interruption-handling
22. Pipecat Interruptions 文档: https://docs.pipecat.ai/pipecat/fundamentals/interruptions
