# 手机端中文命令识别准确率提升研究（Mi Max 2 / sherpa-onnx v2.0.1）

> 结论先行：不改硬件、不上云 ASR 时，**性价比排序 = 修解码/线程/重采样 → 闭集命令解码（复用现有 3M KWS）或离线二遍 SenseVoice → 热词/拼音 FST → 领域微调 → LLM 纠错**。「向前/向后」是成对同词性方向词，热词只是同时给两者加分，**不能定向纠偏**；必须靠闭集候选解码或更强的非流式声学模型。上游 manifest 拟换的 xlarge int8 在 A53 上按公开数据推算 RTF≈3.5–4，**不可用于实时流式**，不建议。

## 0. 现状锚点（用于所有推算）

- 部署链：PulseAudio 48 kHz 单声道 → Python `_resample` → 16 kHz 帧 → 每帧 `accept_asr()`（100 ms/帧）→ 端点后交 LLM/技能。ASR 是**逐帧流式**，RTF>1 会积压帧，直接变成“说完话很久才出结果”。
- 代码现状（[engine.py](../../robotsoul/voice/engine.py)）：`decoding_method="greedy_search"`、`ROBOTSOUL_VOICE_ASR_THREADS` 默认 **1**、端点 rule1=0.5 s / rule2=0.3 s、Silero `threshold=0.5 / min_silence=0.25`、VAD 最长句 20 s。
- 已知实测：ASR RTF≈0.85（2 线程）、TTS RTF≈1.8（2 线程）、整套模型加载 47–54 s。
- A53 换算基准：以下公开 RTF 多在 RK3588 A55/A76 或 Apple 芯片测得，**不能直接当 A53 数字**；按 A55→A53 约 ×1.2–1.5 粗估（估算，待实测）。

## 1. 模型升级候选（含 xlarge 评估）

| 候选（sherpa-onnx 官方） | int8 大小 | 公开 RTF 参考 | A53 现实性 | 说明 |
|---|---:|---|---|---|
| `streaming-zipformer-zh-int8-2025-06-30`（现状，约 160 M 参数） | ~160 MB | 实机 0.85 / 2 线程 | 已上线 | 流式大模型；先确认实机线程数不是默认 1 |
| `streaming-zipformer-zh-xlarge-int8-2025-06-30` | 726 MB encoder + 8.1 MB decoder + 1.5 MB joiner | 0.46（Apple 1 线程，5.6 s 音频） | ❌ RTF≈3.5–4（估算） | manifest 记 598 MB，官方解压后 736 MB（598 MB 或为压缩包体积，建议核对） |
| `streaming-zipformer-ctc-zh-int8-2025-06-30` | 155 MB | 0.14（Apple 1 线程） | ✅ 可做更快的 pass1 | CTC 不支持热词，但可配 HLG 语法（见 §4） |
| `streaming-zipformer-small-ctc-zh-int8-2025-04-01` | 25 MB | 0.038（Apple 1 线程） | ✅ 极轻量草稿/端点 | byte-BPE，转写有小幅丢字 |
| `sense-voice-...-int8-2024-07-17`（非流式） | 228 MB | **A55**：1T 0.436 / 2T 0.260 / 3T 0.208 / 4T 0.175 | ✅ 估 1T≈0.55、2T≈0.33 | 二遍重解码最佳候选，含 ITN |
| `paraformer-zh-int8-2025-10-07`（非流式） | 227 MB | 0.073（Apple 1T，仅横比） | ⚠️ 待实测 | 中文强，适合整句重解 |

**xlarge 是否值得（结论：不值得）**：同配方 CER 收益有限——zipper large→xl 在 aishell-1 dev 为 1.33→1.22、WenetSpeech test-net 7.07→6.89（离线 greedy）；而参数量约 4.4×，按现状 RTF 0.85×4.4≈3.7（2 线程估算），内存约 0.9–1.2 GB RSS、加载时间或再翻数倍（估算，待实测）。**流式必须 RTF≪1**，因此 xlarge 只能排在“离线二遍也用不起”的位置；同等算力预算给 SenseVoice 收益更高。

下载（release tag `asr-models`，把 URL 末段换成对应文件名）：
- xlarge：`https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-zh-xlarge-int8-2025-06-30.tar.bz2`
- pass1 备选：`.../sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30.tar.bz2`、`.../sherpa-onnx-streaming-zipformer-ctc-zh-int8-2025-06-30.tar.bz2`、`.../sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01.tar.bz2`
- pass2：`.../sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2`、`.../sherpa-onnx-paraformer-zh-int8-2025-10-07.tar.bz2`
- 命令闭集 KWS（tag `kws-models`）：`https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2`

## 2. 热词/上下文偏置（sherpa-onnx hotwords）

- **唯一正确用法**：`decoding_method="modified_beam_search"` + `hotwords_file` + `hotwords_score` + `modeling_unit`（`cjkchar`/`bpe`/`cjkchar+bpe`）+（bpe 时）`bpe_vocab`；支持每条热词单独加权 `语音识别 :3.5`；`max_active_paths` 控制候选宽度（默认 4）；也支持按流传入 `create_stream(hotwords="...")`。仅 **transducer**（流式/非流式）支持；greedy 与 CTC/paraformer/SenseVoice 均不支持；实现在 Aho-Corasick 上做路径加分，部分匹配失败会撤销分数，**无需重训**。
- 同一 transducer 还可用浅融合 LM 重打分：`lm=`（RNN LM ONNX）+ `lm_scale` + `lodr_fst`/`lodr_scale`，但需要先训练/导出 LM（中等成本），且 `lm` 也要求 `modified_beam_search`。
- 示例 `hotwords.txt`：`向前走 :2.0`、`向后走 :2.0`、`掉头`、`趴下`。
- **CPU 代价**：`modified_beam_search` 的算力通常为 greedy 的约 1.5–3×（经验值，待实测）；切过去后若 RTF 逼近 1，先把线程加到 2–3，否则会积压帧、延迟反而变差。
- **对“前/后”混淆的效果：有限**。两者都是常用词且都合法，同时进热词只是都加分；热词更适合罕见词/专名（如“掉头”“俯卧撑”）。
- **tokenizer 风险（待验证）**：2025-06-30 系列用 BPE+byte fallback（icefall 说明），而 hotwords 需要 sentencepiece 的 `bpe.vocab`（可由 `bpe.model` 用 `scripts/export_bpe_vocab.py` 导出）；若 tokens 实为 byte-BPE，hotwords 可能不可用。验证法：打印 `tokens.txt` 前 20 行确认建模单元，再用固定音频 A/B `hotwords_score=0 / 2.0`。

## 3. 两遍解码（流式草稿 + 非流式重解码）

- **sherpa-onnx 没有内置 two-pass 管线**；但 `OnlineRecognizer` 与 `OfflineRecognizer` 是两个独立对象，可同进程共存（内存相加）。官方“Small models”页明确建议“小流式模型作 pass1、非流式模型作 pass2”。
- 置信度门控：`get_result_all(s).ys_probs` 给出每 token 的 log 概率（还有 `context_scores`），可只在“低置信/含方向词”时触发 pass2，把平均延迟增量压到很低；公开 API **不返回 n-best**，需要多候选时只能靠双模型/多次解码（或改源码，待验证）。
- 实现：pass1 维持现状（端点/partial）；端点触发时把该句音频（service 已通过 VAD 段拿到）喂 `OfflineRecognizer.from_sense_voice(model=..., tokens=..., num_threads=2/3, use_itn=True, hr_lexicon=..., hr_rule_fsts=...)`，以 pass2 文本覆盖 pass1，pass1 保留用于日志/对比。
- 延迟增量（估算）：2.5 s 语音 × SenseVoice 2 线程 RTF≈0.33 → **+0.8 s**；3–4 线程 → +0.5–0.6 s；叠加端点 0.3 s 后“端点到 final”约 0.8–1.4 s，**会突破现有 700 ms 目标**，需明确取舍。
- 内存/启动：+228 MB 权重（合计约 390 MB），RSS 估 600 MB+（待实测）；加载时间在现状 47–54 s 上再增（建议开机后台预加载，勿首次说话时加载）。
- 收益来源：非流式整句建模 + 更强训练数据（SenseVoice 为中小模型、多语种大数据集），对短命令通常明显优于同量级流式；**具体 CER 增益必须用自采命令集实测**（待验证）。

## 4. 受限命令语法/关键词约束（闭集 → 接近 100 % 的关键）

- **路线 A（首选，零模型成本）：复用现有 3M KWS 做命令闭集**。`sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20`（encoder int8 4.4 MB；chunk-8=160 ms、chunk-16=320 ms 延迟；项目已用 chunk-8）本身就是“只能解码给定关键词的微型 ASR”，把命令表编码成 keywords 即得 **N 选 1 闭集分类**，方向词混淆被候选集约束住。命令：
  `sherpa-onnx-cli text2token --tokens tokens.txt --tokens-type phone+ppinyin --lexicon en.phone keywords_raw.txt keywords.txt`
  `keywords_raw.txt`：`向前走 @向前走` / `向后走 @向后走` / `转圈 @转圈`（每行 `@原文` 必填、不能含空格）。
  代码：`KeywordSpotter(..., keywords_file=..., keywords_score=3.0, keywords_threshold=0.1, num_trailing_blanks=1, max_active_paths=4)`；命中后必须 `reset_stream(s)`；误触发用 `#阈值`、`:加权` 调。
- **路线 B（最强但最贵）：CTC + HLG 语法图**。`OnlineRecognizer.from_zipformer2_ctc(..., ctc_graph="HLG.fst", ctc_max_active=3000)`（传了 `ctc_graph` 后 `decoding_method` 被忽略），模型用 `streaming-zipformer-ctc-zh-int8-2025-06-30`（155 MB）；HLG 需在 PC 上用 Kaldi/lexicon 构建后推到设备。等价于 Vosk 动态语法（Vosk 运行时改词表还需 dynamic-graph 小模型），闭集效果最好、工程量最高（待验证收益）。
- **路线 C（零运行时开销的后处理）：拼音规则 FST（homophone replacer）**。所有中文输出模型、流式/非流式、任意解码方法都支持：`hr_lexicon=lexicon.txt` + `hr_rule_fsts=replace.fst`（pynini 用带调拼音规则生成，如 `xiang4qian2zou3→向前走`）。只能修同音/近音（“响钱”→“向前”），**修不了真正的 前/后 声学混淆**；但可把命令写法归一，配合路线 A 使用。

## 5. 领域微调（icefall）可行性

- 流程：icefall 官方 finetune 教程 `./zipformer/finetune.py --do-finetune 1 --finetune-ckpt pretrained.pt --base-lr 0.0045 --num-epochs 20 --use-mux 0`（低学习率、可选 mux 混原始数据防遗忘）；**建议 GPU**。产出 ONNX 后按现有 manifest 固定哈希与架构。
- 数据成本：每命令 100–300 条 × 10 命令 × 3–5 人/距离/噪声 ≈ 3k–15k 条（1–3 h 音频），必须含最小对立对（同一句位说“向前/向后”）。Vosk 文档称约 1 小时数据可微调声学模型（英文经验，可作为下限参考）。
- 现实性：训练侧 1–2 人周 + 1 张 GPU；风险是数据不足时过拟合/遗忘，且要用同一麦克风与前端条件录音。**先做数据与评测集，再决定是否训练**。

## 6. 前端处理（对 A53 收益/成本都大）

- **重采样**：当前 `_resample` 是每帧纯 Python 线性插值（48k→16k）。优先让 PulseAudio 直接给 16 kHz：`pactl load-module module-remap-source master=<src> source_name=mic16k rate=16000 channels=1`，或改 `default-sample-rate`，或 numpy/polyphase 向量化。省 CPU、也少一次插值失真。
- **增益/定标**：语音 RMS 目标约 −20 dBFS，避免削顶；先 `pactl list sources short` 确认设备与音量，再 `pactl set-source-volume` 定标。不要加激进 AGC（会把噪声一起放大）。
- **降噪**：默认不开；可实验 sherpa-onnx speech enhancement（GTCRN/DPDFNet）或 LADSPA，但降噪可能伤擦音/韵母，对 前/后 未必有益（待验证）。
- **AEC**：仅当 TTS 播报与拾音重叠时需要。PulseAudio：`pactl load-module module-echo-cancel aec_method=webrtc source_master=<src> sink_master=<sink>`（postmarketOS 的 PulseAudio 已启用 webrtc，MR 4480）；AEC3 在 A53 有 CPU 成本（待实测）。半双工（播报时闭麦）则不必开。
- **VAD/端点**：现 `threshold=0.5、min_silence=0.25`、端点 0.5/0.3 s 已偏低延迟；若“说完到出结果慢”，先分段计时：VAD 端点 / ASR 解码 / LLM / TTS，别把 TTS RTF 1.8 算进 ASR。

## 7. LLM 语义纠错（DeepSeek Flash）

- 用法：输入 = ASR 文本（可加 pass1+pass2 双候选与 `ys_probs` 置信度）+ 命令枚举 + 技能 schema，`response_format={"type":"json_object"}`（指南要求 prompt 里含 “json” 并给示例），输出 `{"command":"forward","confidence":0.9}`；非法/低置信/超时则回退原文或复问。
- 收益：能修近义/漏字/多字（“向前走” → `forward`），但**当两个方向词都合法时 LLM 没有声学证据**，无法可靠纠 前/后，反而有改错风险。
- 成本：约 300 input + 30 output tokens；`deepseek-flash` 峰值 cache-miss $0.3/1M input、$1.2/1M output → 单次 ≈ $0.0001，可忽略。延迟 = 网络 RTT + 首 token（未公开，待实测，通常 0.3–1.5 s），会成为新瓶颈；必须设 800 ms 级超时并旁路。
- 更便宜的第一步：**本地拼音编辑距离/同音表匹配**（<1 ms、免费）先吃掉大部分简单错误，LLM 只处理残余低置信样本。

## 8. 推荐路线（按性价比/实施成本排序）

| 阶段 | 动作 | 预期收益 | 代价 | 验证 |
|---|---|---|---|---|
| P0（0.5–1 天） | 线程 2–3；`modified_beam_search`+`max_active_paths=4`；重采样移到 PulseAudio；VAD/ASR/LLM 分段计时 | 消除解码积压（RTF 0.85→约 0.6，估算），延迟可解释 | 几乎零 | 命令集准确率基线 + 混淆矩阵 + RTF/P95 |
| P1（1–3 天） | 3M KWS 命令闭集（复用已加载模型，+0 内存，+160 ms 级） | 闭集命令/方向词大幅提升（最可能接近 100 %） | 需设计关键词与阈值；有误触发 | 200 条实录：命中率/误触发率/A-B |
| P2（2–5 天） | 二遍 SenseVoice int8（+228 MB、+0.5–0.9 s P95） | 整句 CER 与方向词改善 | 内存/加载时间上升；破 700 ms 目标 | 同音频 A/B：pass1 vs pass2 |
| P3（0.5 天） | 热词（验证 tokenizer 后）+ 拼音 FST 归一 | 罕见词/同音词改善；前后无效 | 很小 | hotwords_score 0/2 A/B |
| P4（1–2 周） | icefall 微调（先录 1–3 h 数据） | 领域/麦克风适配，上限最高 | GPU + 数据 + 数据集维护 | 留出集 CER、回归原始能力 |
| P5（可选） | DeepSeek Flash 纠错（旁路、超时回退） | 尾部长尾修复 | 网络延迟/幻觉风险 | 纠错准确率、误改率、P95 增量 |

## 9. 验证方法（统一口径）

- 数据集：10 条命令 × 20 人 × 3 距离/噪声条件 ≈ 600 条（含“向前/向后”同句位最小对立对），固定录音脚本与麦克风位置。
- 指标：命令集准确率（目标 ≥98 %）、混淆矩阵（前/后单元格）、帧级 RTF、端点到 final transcript P95、误唤醒/误触发率、RSS、10 h 长跑稳定性；A/B 必须用同一批音频与同一前端。

## 参考 URL

- sherpa-onnx 官方文档：流式 transducer [models](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/zipformer-transducer-models.html)、流式 CTC [models](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-ctc/zipformer-ctc-models.html)、[Small models（two-pass 建议）](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/small-online-models.html)、[SenseVoice（含 RK3588 RTF 表）](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)、[离线 Paraformer](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-paraformer/paraformer-models.html)
- [Hotwords/上下文偏置](https://k2-fsa.github.io/sherpa/onnx/hotwords/index.html)、[KWS 原理与 keywords 文件](https://k2-fsa.github.io/sherpa/onnx/kws/index.html)、[KWS 预训练模型（3M）](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)、[拼音词组匹配替换](https://k2-fsa.github.io/sherpa/onnx/homophone-replacer/index.html)、[Silero VAD](https://k2-fsa.github.io/sherpa/onnx/vad/silero-vad.html)、[Speech enhancement](https://k2-fsa.github.io/sherpa/onnx/speech-enhancement/index.html)
- 代码/参数实证：[online-decode-files.py](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/master/python-api-examples/online-decode-files.py)（hotwords/lm/lodr 参数）、[keyword-spotter.py](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/master/python-api-examples/keyword-spotter.py)、[StreamingDecodeFileCtcHLG.java](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/master/java-api-examples/StreamingDecodeFileCtcHLG.java)、[vad-with-non-streaming-asr.py](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/master/python-api-examples/vad-with-non-streaming-asr.py)
- 训练/精度：[icefall multi_zh-hans 结果（large/xl CER）](https://raw.githubusercontent.com/k2-fsa/icefall/master/egs/multi_zh-hans/ASR/RESULTS.md)、[Zipformer 微调教程](https://raw.githubusercontent.com/k2-fsa/icefall/master/docs/source/recipes/Finetune/from_supervised/finetune_zipformer.rst)
- 前端/系统：[Vosk 模型适配（词表/语法/微调）](https://alphacephei.com/vosk/adaptation)、[PulseAudio module-echo-cancel](https://www.freedesktop.org/wiki/Software/PulseAudio/Documentation/User/Modules/#module-echo-cancel)、[postmarketOS PulseAudio 启用 webrtc (MR 4480)](https://gitlab.postmarketos.org/postmarketOS/pmaports/-/merge_requests/4480)
- LLM：[DeepSeek 模型与价格](https://api-docs.deepseek.com/quick_start/pricing)、[JSON Output](https://api-docs.deepseek.com/guides/json_mode)
