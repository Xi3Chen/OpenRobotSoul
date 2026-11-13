# MiMax2 语音部署进度

更新时间：2026-11-13（下午继续）

本文记录当前一次 MiMax2 Linux 手机部署和语音功能验证的状态。SSH 密码等敏感信息不写入仓库。

## 已完成

- 已确认远程设备可通过 `user@192.168.1.8` 登录。
- 已确认设备为 Alpine Linux、`aarch64`，内核为 MSM8953。
- 已确认手机内置麦克风可以通过 PulseAudio 采集；此前短时采集约 2 秒得到 20 个音频帧，采样率为 48 kHz。
- 已确认现有手机服务 `voice-assistant.service` 正在运行，并长期占用 PulseAudio 麦克风。它使用 Vosk 小模型，已经连续运行超过一天，日志显示 CPU/RSS 稳定，静音状态没有唤醒。
- 已确认 sherpa-onnx 官方 manylinux ARM64 wheel 不适合该 Alpine/musl 环境；已在手机上使用固定版本源码编译出可加载的 musl Python 扩展，位置为 `/home/user/mimax2-voice/runtime/sherpa-musl`。
- 已确认 KWS、流式 ASR、VAD、中文 Xiao-Ya TTS 模型可以初始化；静音输入没有误触发唤醒。
- 已确认 MiMax2 闪光灯适配器工作。设备路径为 `/sys/class/leds/white:flash/brightness`，实测可在亮度 `4` 和 `0` 之间切换；驱动优先调用手机已有的 `/usr/local/sbin/voice-assistant-flashlight on|off`。
- 已实现并通过针对性测试：状态机、捕获租约、通话抢占、本地麦克风/网页通话互斥、断线重连、旧 generation 音频丢弃、无麦克风休眠、资源清单校验、MiMax2 闪光灯适配器和任务 watchdog，共 `18 passed, 1 skipped`（此前结果）。
- 本机项目的 `build/` 已迁移到 `D:\work-msm8953\RobotHarness-build`，项目内保留目录连接；D 盘仍有约 73 GB 可用。

## 当前部署状态

- 手机项目目录：`/home/user/mimax2-voice`。
- 手机 musl 运行库：`/home/user/mimax2-voice/runtime/sherpa-musl`，约 10 MB。
- 当前 sherpa 模型仍位于 `/tmp/mimax2-voice-resources`，约 293 MB；该目录属于 tmpfs，重启后会丢失，不能作为最终部署位置。
- 手机根分区约 3.9 GB，剩余约 204 MB；根分区已经接近满载，后续不能再把大型构建目录或重复模型包复制到 `/`。
- `/dev/mmcblk0p66` 是一个未挂载的大分区，已开始探测其文件系统，但尚未确认可以安全挂载；确认前不要写入或格式化。
- 手机当前有另一个已启用的 `voice-assistant.service`，主进程 PID 为 816，子进程 `parecord` 正在占用内置麦克风。因此，不能在不处理音频占用的情况下同时启动本项目的本地采集服务。
- `sudo -l` 显示用户可以执行闪光灯专用脚本的免密命令，也拥有普通 sudo 权限；仓库中提到的 `/etc/sudoers.d/mimax2-flash` 当前不存在，`visudo -cf /etc/sudoers` 已报告主配置可解析。不要重新创建带字面量 `\\n` 的临时 sudoers 文件。

## 代码和资源注意事项

- 不要继续使用 `scripts/install_alpine_sherpa.py` 的 manylinux wheel 替换方案；该方案在 Alpine/musl 上已验证会失败。它需要改成明确的 Alpine 原生构建/安装流程，或文档化手机上已经验证的 musl 构建流程。
- 第三方 sherpa-onnx 固定在子模块 commit `af2d8d81dbfa61c4cea4c568da78f3982edca005`（`v2.0.1`）。
- 模型、wheel 和运行库不提交 Git；继续使用资源 manifest 和下载/安装/校验/打包/导入脚本。
- `pytest -q` 不应从仓库根目录递归收集 sherpa 子模块自带脚本。项目测试应使用：

  ```powershell
  $env:PYTHONPATH = "."
  python -m pytest -q tests
  ```

- 之前直接运行 `pytest -q tests` 时，Windows Python 没有自动把仓库根目录加入导入路径，出现 `ModuleNotFoundError`；这是测试环境入口问题，不是语音代码失败。设置 `PYTHONPATH=.` 后已开始重新运行测试，结果需下午重新确认。

## 下午继续顺序

1. 只读确认 `/dev/mmcblk0p66` 的文件系统类型和可用空间；如果它有可用文件系统，挂载到独立目录并把模型迁移过去。若不可用，保持模型在 tmpfs，先完成验证并明确重启丢失风险。
2. 检查并记录现有 `voice-assistant.service` 的音频占用；停止它后再启动本项目 Voice Service，或明确选择现有服务作为唯一本地麦克风占用者，不能让两个服务抢同一个源。
3. 用持久运行库和模型运行本项目 `VoiceService`：被动监听、唤醒、一次中文命令识别、TTS 合成和扬声器播放；记录每段耗时和 RSS。
4. 清理/修正 Alpine 安装脚本，删除基于 manylinux wheel 的误导性路径，保留可复现的 musl 原生构建说明。
5. 运行完整项目测试：

  ```powershell
  $env:PYTHONPATH = "."
  python -m pytest -q tests
  ```

  必要时使用 `--ignore=third_party`，但不能把失败测试静默忽略。
6. 配置一个只读状态检查或 watchdog，定期记录服务 PID、音频状态、模型加载状态、队列深度、RSS、文件描述符和根分区余量；watchdog 只报告过期，不自动杀进程。
7. 最后再做短时混合负载和长跑准备。当前还没有完成 10 小时稳定性验收，也没有完成浏览器 WebRTC 通话实机验收。

## 当前明确风险

- 手机根分区余量不足，模型持久化位置未解决前不能把当前部署视为可重启部署。
- 现有 Vosk 服务和本项目 sherpa 服务存在麦克风独占冲突。
- 目前只完成了短时采集、模型初始化和单元/契约测试；不能据此宣称满足 10 小时连续使用、P95 延迟门槛或通话重连验收。
- Xiao-Ya TTS 模型卡为非商业用途许可；商业发布前必须替换并重新核对许可证。
