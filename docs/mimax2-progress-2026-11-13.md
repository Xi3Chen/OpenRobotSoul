# MiMax2 语音部署进度

更新时间：2026-11-13（傍晚收尾）

本文记录当前一次 MiMax2 Linux 手机部署和语音功能验证的状态。SSH 密码、DeepSeek API key 等敏感信息不写入仓库。

## 本轮关键结论

- 手机磁盘已迁移到最大分区 `/dev/mmcblk0p66`（52.5 GB，`userdata`），项目与语音模型都落在该分区；根分区从可用 ~204 MB 提升到 ~423 MB，`/tmp` 从 293 MB 回落到 ~10 MB。
- 本项目 VoiceService 已作为 systemd 服务常驻：`mimax2-voice.service` + 只读 watchdog，当前 PASSIVE 监听、麦克风 RUNNING、模型已加载、RSS ~445 MB、空闲 CPU ~35%（单核）。
- 语音 Agent 使用上游的常驻语言网关（OpenAI 兼容）接入 DeepSeek 官方 API 的 `deepseek-flash`（DeepSeek-V4.1-Flash-1017）；API key 只保存在手机 `config/llm.json`（0600 user），不进仓库。
- **真机端到端已验收通过**：电脑扬声器播放“索拉 → 打开手电筒”，手机完成唤醒 → ASR → DeepSeek 意图 → 闪光灯点亮（LED 读回 4）→ 中文 TTS 播报；并增加了回声抑制，手机不会把自己的播报再当命令。
- 本机完整测试：`207 passed, 5 skipped`（5 个 skip 是 POSIX 文件权限位断言在 Windows 上不适用）。

## 上游更新合并（2026-11-13 夜间）

- 已 `git fetch` 并快进 `main` 到 `769109e`：上游新增 10 个提交（语言网关/语言会话、Dashboard 账号与设置页、任务运行日志 runlog、音频电平监控、Go2 动作与电池能力、`start-dashboard.sh` 等，约 5700 行）。
- 本地 MiMax2/DeepSeek 工作先提交到分支 `local/mimax2-deepseek-2026-11-13`（commit `63ebcb9`），再合并回 `main`；冲突集中在 `robotsoul/voice/{engine,service,factory}.py` 与 `tests/test_voice_engine.py`，已按“保留上游结构 + 重新加回 MiMax2 需要的扩展”解决：
  - 引擎采用上游的唤醒参数与 ASR 端点修复（`rule3=20s`），保留并扩展 `ROBOTSOUL_VOICE_KWS_*`/`ASR_THREADS`/`TTS_THREADS` 环境变量覆盖。
  - 服务保留上游的 runlog/电平监控/识别观察，加回“中文播报映射”“播报后丢弃回声”和 `queue_depth`/`engine_loaded` 状态属性。
  - 上游 `DemoAgent` 不再做中文关键词匹配，改为由常驻语言网关调 LLM；因此本地的 `robotsoul/deepseek_agent.py` 直连实现已删除，统一走 `config/llm.json` + `robotsoul/language_gateway.py`。
- 子模块 `third_party/picoclaw` 被上游固定到一个远端不存在的提交 `30b6a98…`（`upload-pack: not our ref`），当前本地 checkout 仍是 `bbf6893`；不影响 Python/语音测试，但 `git submodule update` 会失败，需上游修正或本地保留现有 checkout。

## 磁盘迁移（2026-11-13 下午）

- 迁移前：`/dev/mmcblk0p65` 根分区 3.9 GB，已用 3.4 GB，可用 204 MB；`/tmp`（tmpfs）被 `/tmp/mimax2-voice-resources` 占用 293.4 MB。
- `/dev/mmcblk0p66` 探明：无顶层文件系统，内部是旧 pmOS 安装镜像残留（MBR + `pmOS_boot` ext4 子分区 + 2026-10-09 的日志），不是可挂载的现成数据盘；整体 52.5 GB 可用。只备份前 4 KB 到 `/home/user/p66-mbr-backup.bin`。
- `mkfs.ext4 -F -L mimax2-data -m 1 /dev/mmcblk0p66`，UUID `d102330f-e918-477c-bd33-405b35bb55eb`；`/etc/fstab`：
  `UUID=d102330f-e918-477c-bd33-405b35bb55eb /home/user/mimax2-data ext4 defaults,noatime 0 2`
- 迁移内容（先 `cp -a` 校验大小/条目/diff 后再删源）：
  - `/home/user/mimax2-voice` → `/home/user/mimax2-data/mimax2-voice`，原路径保留符号链接（venv 绝对路径 shebang 继续可用）。
  - `/tmp/mimax2-voice-resources`（293 MB 模型，重启会丢）→ `/home/user/mimax2-data/voice-resources`。
- 迁移后：根分区可用 ~423 MB，数据分区 51.4 GB、可用 ~50.4 GB；服务单元用 `RequiresMountsFor=/home/user/mimax2-data`，数据分区未挂载时不会启动。

## 当前部署状态

- 项目目录：`/home/user/mimax2-voice`（符号链接）→ `/home/user/mimax2-data/mimax2-voice`。
- musl 运行库：`/home/user/mimax2-voice/runtime/sherpa-musl`（sherpa-onnx v2.0.1 源码构建）。
- 语音资源：`/home/user/mimax2-data/voice-resources`，由 `ROBOTSOUL_VOICE_RESOURCE_ROOT` 指向。
- 服务单元（仓库副本 `deploy/`）：
  - `mimax2-voice.service`：`scripts/run_voice_service.py`，`Restart=always`，`MemoryMax=900M`，`CPUQuota=250%`，`EnvironmentFile=-/etc/mimax2-voice.env`。
  - `mimax2-voice-watchdog.service`：只读 watchdog，`--stale-after 180`，只上报不杀进程。
- 运行状态：`/run/mimax2-voice/{status.json,pid,heartbeat,watchdog.txt}`。`status.json` 含 agent、音频后端/默认源、voice_state、model_loaded、queue_depth、dropped_frames、最近唤醒/转写文本与结果、RSS、fd、根分区/数据分区余量。
- DeepSeek 配置：`config/llm.json`（0600 user，`provider=openai-compatible`、`base_url=https://api.deepseek.com`、`model=deepseek-flash`、`api_key=…`），服务单元用 `ROBOTSOUL_LLM_CONFIG` 指向它；该文件不存在时服务回退到内置 `DemoAgent`（自由文本不再本地匹配，仍可用 `/技能` 直接指令）。
- 语言网关：上游新增 `robotsoul/language_gateway.py`（常驻子进程，读 `config/llm.json`，OpenAI 兼容 chat/completions，无文件/Shell/机器人工具）与 `robotsoul/language_session.py`；voice runner 启动 `LanguageSession` 并交给 `LanguageAgent`。
- 旧的 Vosk 服务 `voice-assistant.service` 已停止，12:45 有另一个 SSH 会话将其 disable 并移除单元；当前不再占用麦克风。`/opt/voice-assistant` 文件仍在根分区（约 67 MB）。
- PulseAudio 默认源：`alsa_input.platform-c051000.sound-card.HiFi__Mic1__source`，48 kHz 单声道。
- sudo：已重建 `/usr/local/sbin/voice-assistant-flashlight`（0755）与 `/etc/sudoers.d/mimax2-flash`（0440，NOPASSWD 仅限 on/off，`visudo -cf` 通过）。

## 本轮修复的缺陷（均已部署并验证）

1. **ASR 端点配置错误导致转写永远为空**：`rule3_min_utterance_length=0.0` 时 sherpa `is_endpoint()` 在每个 0.1 s 分块都返回真，`accept_asr` 连续重置流。改为 `enable_endpoint_detection=False`，由 Silero VAD 断句。
2. **VAD API 不兼容**：sherpa-onnx 2.0.1 的 `VoiceActivityDetector.front` 是只读属性而非方法，`drain_speech_segments()` 在端点抛 `TypeError`；已兼容两种绑定。
3. **闪光灯命令一律失败**：adapter 的 SUCCEEDED feedback 缺 `evidence["condition_met"]`，runtime 判为 “Completion evidence is missing”。现驱动写后读回 `brightness`，adapter 写入 `condition_met`/`brightness`；读回不一致会报错，`driver.close()` 改为 best-effort。
4. **闪光灯 helper 与 sudoers 缺失**：重建后 `打开/关闭手电筒` 在 runtime 路径上 `completed`，LED 读回 4/0。
5. **中文 TTS 念英文运行日志**：runtime 结论文本是英文，Xiao-Ya 中文 TTS 全按 OOV 丢弃；服务对非中文结论改说中文固定回复（成功“好的，已完成。”，失败“抱歉，这个操作没有成功。”）。
6. **systemd 首次启动 PulseAudio 探测失败**：手机 locale 为 zh_CN，`pactl info` 输出“默认源：”；runner 已强制 `LC_ALL=C`。
7. **回声自激**：播报被麦克风拾取后会被当成新命令（实测出现 `好的已完成` 被二次识别）。服务现在在扬声器播报结束后丢弃已缓冲的麦克风音频并重置 ASR/VAD，避免自激。
8. **KWS 唤醒灵敏度**：默认阈值 `#0.6` 对合成音（edge-tts 神经音色）不触发；新增 `ROBOTSOUL_VOICE_KWS_THRESHOLD`（手机当前设为 `0.3`）与 `ROBOTSOUL_VOICE_KWS_SCORE`，真人声在 0.6 下已验证可命中。
9. **唤醒常驻 CPU 过高**：KWS 双线程时 ONNX Runtime 线程空转把空闲 CPU 推到 ~250%；改成 KWS 单线程（`ROBOTSOUL_VOICE_KWS_THREADS`，默认 1）后空闲 CPU 降到 ~35%（单核），ASR 仍默认 2 线程以压低命令识别延迟。
10. **Windows 上 watchdog 会杀掉被探测进程**：`os.kill(pid, 0)` 在 Windows 会 TerminateProcess；`_pid_alive` 改为 Windows 用 `OpenProcess` 探测，POSIX 保持 `os.kill`。
11. **对话 Agent 接入方式调整**：先本地实现了 `robotsoul/deepseek_agent.py` 直连 DeepSeek；拉取上游后改用上游自带的常驻语言网关（`language_gateway` + `language_session` + `config/llm.json`），直连实现已删除，避免两套 LLM 集成并存。
12. **仅说唤醒词被当成命令**：拉取上游后 `DemoAgent`/语言网关会把 “索拉” 本身当作指令提交并回一句“无法执行”。服务现在忽略与配置唤醒词完全相同的转写，保持会话窗口等后续指令；实测“索拉 →（停顿 1.2 s）→ 打开手电筒”可正常执行。

## 验收证据（2026-11-13 傍晚）

- **DeepSeek 实机 API 校验**（手机上直接调用）：
  `model: deepseek-flash`，`endpoint: https://api.deepseek.com/chat/completions`；
  `打开手电筒` → `flash.toggle{enabled:true}`；`关闭手电筒` → `{enabled:false}`；无关问题 → `None`。
- **端到端真机**（电脑扬声器播放 edge-tts 神经中文音色）：
  - 14:13:39 唤醒 `索拉`；
  - 14:13:43 转写 `朵拉打开手电筒`（含唤醒词尾音）；
  - 14:13:46 runtime `completed / success=true`，sudo 调用 helper `on`，LED 读回 `4`；
  - 中文 TTS 播报“好的，已完成。”；开启回声抑制后没有再出现把播报当命令的二次识别。
- **KWS 能力边界**：真实人声（模型自带测试 wav）关键词 `介绍` 在阈值 0.6 命中；edge-tts 神经音色需要 0.3；Windows SAPI Huihui 合成音即使 0.3 也不命中——唤醒验收必须以真人或自然神经音色为准。
- **性能**（MiMax2，单核 A53 级别）：模型加载 47–54 s；KWS RTF≈0.35（1 线程）；ASR RTF≈0.85（2 线程）；TTS RTF≈1.8（2 线程，1.9 s 音频约 3.3 s 合成）；常驻 RSS 445 MB（峰值约 480 MB）。
- **本机测试**：`207 passed, 5 skipped`。注意 DSH Windows 沙箱下需把 `TMP/TEMP` 指向非 junction 的 D 盘目录，否则临时目录 ACL 会造成环境性失败；5 个 skip 是上游针对 POSIX 文件模式（0600/0700）的断言，Windows 无法表达这些权限位。
- **拉取上游后的真机复测**：
  - CLI 走语言网关：`run --llm-config config/llm.json "打开手电筒"` → `task.evaluation.success=true`、证据 `MiMax2 flashlight on`（helper 实际 on 后随 CLI 退出 off）；`"今天天气怎么样"` → `task.failed`，理由由 DeepSeek 生成：“可用技能只有手电筒开关控制，无法查询天气。”
  - 语音端到端：14:52:53 唤醒 `索拉`；14:52:54 转写 `索拉`（被忽略，不提交 runtime）；14:52:57 转写 `打开手电筒`；14:52:59 `completed/success=true`，LED 读回 4；服务保持 PASSIVE，watchdog `state=running`。
  - 合并后空闲 CPU ≈45%（单核，KWS 单线程），RSS ≈449 MB。
  - 注意：上游 `config/voice.json` 已把 ASR 指向 xlarge int8（约 598 MB）；手机仍保留原来的小模型目录 `models/asr-zh-streaming-int8`（引擎按目录与文件名加载，功能正常），但若在手机上跑 `voice-resources verify` 会因清单版本不一致报错。

## 剩余工作与明确风险

- **低延迟/识别质量调研已完成（2026-11-14）**：综合报告见 [low-latency-voice-research-2026-11-14.md](low-latency-voice-research-2026-11-14.md)，分项笔记在 `docs/research/`（ASR 准确率、低延迟对话、打断与全双工）。核心结论：延迟大头是整句 TTS 与非流式 LLM；闭集命令可用现有 KWS 做到 10/10；模板回复 TTFA 可到 150–300ms，开放回复受 Xiao-Ya TTS 限制约 1–1.5s；AEC3+分层打断可行，端到端全双工在 A53 不可行。

- **10 小时稳定性验收未做**：目前只有约 30 分钟常驻数据（RSS/CPU 稳定），需要长跑记录唤醒率、误唤醒、内存/温度与根分区余量。
- **时钟偏差**：手机时钟比本机快约 13 s（未确认 NTP），跨设备时间对齐的验收数据需要用同一时钟解释；建议后续开启时间同步。
- **无语学回声消除（AEC）**：当前用“播报后清空麦克风缓冲 + 半双工”规避自激；真正的免打断/打断体验需要 AEC 或播报期间暂停采集。
- **浏览器 WebRTC 通话实机验收未做**（本轮未启动 dashboard/媒体桥）。
- **唤醒阈值 0.3 偏敏感**：需在真实环境观察误唤醒；真人正常说话可先试 0.6，合成音/轻声再降到 0.3。
- 旧 `/opt/voice-assistant`（Vosk）文件仍占根分区约 67 MB，已停止且单元被移除；如需腾空间可归档到数据分区，但属于另一个项目，未擅自删除。
- 12:45 检测到另一个 SSH 会话（192.168.1.3，公钥登录）对手机执行了停止/删除旧服务操作；本轮未与其冲突，后续并发改动手机前应先确认。
- DeepSeek API key 位于手机 `config/llm.json`（0600 user）；轮换 key 时更新该文件并 `systemctl restart mimax2-voice` 即可，不要写入仓库或日志。
- Xiao-Ya TTS 模型卡为非商业用途许可；商业发布前必须替换并重新核对许可证。
- 系统 locale 为 zh_CN，解析命令行工具输出的脚本必须显式 `LC_ALL=C`（runner 已处理）。
