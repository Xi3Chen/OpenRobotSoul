# OpenRobotSoul 开发与部署指南

本文按当前 `main` 分支编写，覆盖 Windows 开发、常见 Linux 发行版依赖安装，以及在 aarch64 Go2 主机上构建和验证只读状态 bridge。

**能力边界：**当前版本只连接 Unitree SDK 并读取 `rt/sportmodestate`，Go2 profile 的 `capabilities` 为空。它不会让机器人站立、移动或执行其他动作。`status` 通过只表示只读通信链路可用，不代表动作安全已经验证。

## 1. 环境选择

| 场景 | 推荐环境 | 结果 |
| --- | --- | --- |
| Windows 开发 | Windows 10/11、Python 3.11+ | Python 测试、模拟器、Dashboard、wheel |
| Linux x86_64 开发机 | Ubuntu/Debian、Fedora、Arch 等 | 构建 x86_64 SDK 示例和只读 bridge |
| Go2 实机 | Linux aarch64 | 在机器人本机编译并运行 aarch64 只读 bridge |

SDK checkout 目前只包含 Linux `x86_64` 和 `aarch64` 预编译库。Windows/macOS 不能直接链接这些 Linux `.so`。Windows 可用 WSL 做 Linux x86_64 编译验证，但 x86_64 产物不能部署到 aarch64 机器人。最简单可靠的 aarch64 路径是在机器人本机编译。

## 2. Windows 开发

### 2.1 安装工具

在 PowerShell 中运行：

```powershell
winget install --id Python.Python.3.12 --exact
winget install --id Git.Git --exact
winget install --id Kitware.CMake --exact
winget install --id GoLang.Go --exact
winget install --id Microsoft.VisualStudio.2022.BuildTools --exact
```

打开 Visual Studio Installer，为 Build Tools 勾选 **Desktop development with C++**，完成后重开 PowerShell。确认 Go 为 1.25.13 或更新：

```powershell
python --version
git --version
cmake --version
go version
```

### 2.2 克隆并初始化

```powershell
git clone --recurse-submodules https://github.com/Xi3Chen/OpenRobotSoul.git
Set-Location OpenRobotSoul
git submodule update --init --recursive
```

### 2.3 安装 Python 环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

如果激活脚本被系统策略拦截，直接调用 `.\.venv\Scripts\python.exe` 即可，不需要更改执行策略。

### 2.4 运行开发检查

```powershell
python -m unittest discover -s tests -v
python -m robotsoul profiles
python -m robotsoul --robot-module simulator demo
python -m robotsoul --robot-module simulator status
```

Windows 上可以构建 Python wheel：

```powershell
python -m pip wheel . --no-deps --wheel-dir dist
```

wheel 只包括 Python 代码和静态资源，不包括 Linux bridge、DDS `.so` 或 Unitree 库。不要在 Windows 上运行完整 Go2 原生构建；当前 Linux SDK 库不兼容 Windows。`python -m robotsoul.build --platform windows --arch x86_64` 会明确报 SDK 平台不支持，这是预期行为。

## 3. Linux 安装依赖

所有发行版都需要 Python 3.11+、Git、CMake、C++17 编译器和 Go 1.25.13+。下面同时安装当前 SDK 示例所需的 C++ 开发库。

### 3.1 Ubuntu / Debian

```bash
sudo apt-get update
sudo apt-get install -y \
  git python3 python3-venv python3-pip \
  cmake build-essential \
  libyaml-cpp-dev libeigen3-dev libboost-all-dev libfmt-dev
```

### 3.2 Fedora

```bash
sudo dnf install -y \
  git python3 python3-pip \
  cmake gcc-c++ make \
  yaml-cpp-devel eigen3-devel boost-devel fmt-devel
```

Rocky/AlmaLinux 等兼容系统可能需要先启用包含开发包的仓库；随后安装对应的 `cmake`、`gcc-c++`、yaml-cpp、Eigen、Boost 和 fmt 开发包。

### 3.3 Arch Linux

```bash
sudo pacman -Syu --needed \
  git python python-pip \
  cmake base-devel \
  yaml-cpp eigen boost fmt
```

### 3.4 检查版本

```bash
python3 --version
git --version
cmake --version
c++ --version
go version
```

如果 Go 低于 1.25.13，请从 [Go 官方下载页](https://go.dev/dl/) 下载安装对应 Linux 架构的发行包。以下是 aarch64 示例；x86_64 文件名将 `arm64` 换为 `amd64`。安装到独立版本目录，不覆盖系统已有的 Go：

```bash
sudo mkdir -p /opt/go1.25.13
sudo tar -C /opt/go1.25.13 --strip-components=1 -xzf go1.25.13.linux-arm64.tar.gz
echo 'export PATH=/opt/go1.25.13/bin:$PATH' >> ~/.profile
export PATH=/opt/go1.25.13/bin:$PATH
go version
```

## 4. Linux 获取源码

在 Linux 构建机或机器人上：

```bash
sudo mkdir -p /opt
sudo chown "$USER" /opt
git clone --recurse-submodules https://github.com/Xi3Chen/OpenRobotSoul.git /opt/OpenRobotSoul
cd /opt/OpenRobotSoul
git submodule update --init --recursive
test -f adapters/unitree_sdk2/CMakeLists.txt
test -f third_party/picoclaw/go.mod
```

若 SDK 目录缺失：

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

## 5. Linux x86_64 开发机

以下命令在 x86_64 Linux 主机上构建 x86_64 组件，不生成 aarch64 产物：

```bash
cd /opt/OpenRobotSoul
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[voice]"
python -m unittest discover -s tests -v
python -m robotsoul.build \
  --platform linux --arch x86_64 \
  --build-type Release --jobs "$(nproc)"
```

产物位于 `build/linux-x86_64/`：`bin/` 为 SDK 示例和 PicoClaw，`lib/` 为 Go2 bridge、DDS 和 Unitree 运行库，`manifest.json` 记录构建清单。

## 6. Go2 aarch64 实机部署

本节按在机器人本机编译编写。跨编译需要额外配置 aarch64 C++ toolchain 和匹配的所有库，不是本指南的默认流程。

### 6.1 确认系统、架构和网卡

```bash
uname -m
cat /etc/os-release
ip -br link
ip -br addr
```

`uname -m` 应为 `aarch64`。记录连接 Go2 的网卡真实名称，例如 `eth0` 或 `enP2p1`，不要假设固定名称。检查机器人网络连通性，并避免多个程序同时初始化/使用同一个 Unitree DDS 通道。

### 6.2 安装依赖并获取项目

按第 3 节选择 Ubuntu/Debian、Fedora 或 Arch 的命令，再按第 4 节克隆源码。确认 `python3` 至少为 3.11，Go 至少为 1.25.13。

### 6.3 安装 Python 环境并本机构建

```bash
cd /opt/OpenRobotSoul
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m unittest discover -s tests -v
python -m robotsoul.build \
  --platform linux --arch aarch64 \
  --build-type Release --jobs "$(nproc)"
```

构建器把程序和依赖放到 `build/linux-aarch64/`。如果提示 SDK 不支持 `linux/aarch64`，检查 submodule 和这些文件：

```bash
ls -l adapters/unitree_sdk2/lib/aarch64/libunitree_sdk2.a
ls -l adapters/unitree_sdk2/thirdparty/lib/aarch64/libddsc.so*
ls -l adapters/unitree_sdk2/thirdparty/lib/aarch64/libddscxx.so*
```

### 6.4 指定连接网卡

在当前终端先设置网卡名（替换成第 6.1 步实际查到的名称）：

```bash
export OPENROBOTSOUL_GO2_NETWORK_INTERFACE=eth0
```

为了后续登录仍可使用，可建立仅当前用户可读的本地配置文件：

```bash
umask 077
printf '%s\n' 'OPENROBOTSOUL_GO2_NETWORK_INTERFACE=eth0' > ~/.openrobotsoul-go2.env
set -a
. ~/.openrobotsoul-go2.env
set +a
```

后续每次登录后再次执行 `set -a; . ~/.openrobotsoul-go2.env; set +a`。当前项目没有安装 systemd 服务；本指南按需要时手动运行 `status` 编写。

若自定义了 bridge 位置，再设置其完整路径：

```bash
export OPENROBOTSOUL_GO2_BRIDGE_LIBRARY=/opt/OpenRobotSoul/build/linux-aarch64/lib/libopenrobotsoul_go2_bridge.so
```

### 6.5 检查动态库并验证只读状态

```bash
cd /opt/OpenRobotSoul
ldd build/linux-aarch64/lib/libopenrobotsoul_go2_bridge.so
python -m robotsoul status
```

`ldd` 不应显示 `not found`。如需检查，可以确认以下文件都在同一个 `lib/` 目录：

```text
libopenrobotsoul_go2_bridge.so
libddsc.so  libddsc.so.0
libddscxx.so  libddscxx.so.0
```

成功的 `status` 输出应包含 `"connected": true`，且 `observations` 中有 `robot.sport_state`。`capabilities` 应为空数组，这是当前版本的预期值。

## 7. 使用 wheel 部署

实机优先使用第 6 节的源码 checkout。本项目 wheel 不含平台相关 native 文件；若必须通过 wheel 部署，需要另外带上 bridge 和 DDS 动态库：

1. 在目标 aarch64 环境生成/取得匹配的 wheel；启用语音时按目标 Python 版本安装 `voice` extra 及其依赖。
2. 在目标机创建 Python 环境并安装：

   ```bash
   python3 -m venv /opt/openrobotsoul-runtime/.venv
   . /opt/openrobotsoul-runtime/.venv/bin/activate
   python -m pip install "/path/to/openrobotsoul-*.whl[voice]"
   ```

3. 把整个 `build/linux-aarch64/lib/` 复制到目标机同一目录，例如 `/opt/openrobotsoul-runtime/native/`。
4. 指向 bridge 并设置网卡：

   ```bash
   export OPENROBOTSOUL_GO2_NETWORK_INTERFACE=eth0
   export OPENROBOTSOUL_GO2_BRIDGE_LIBRARY=/opt/openrobotsoul-runtime/native/libopenrobotsoul_go2_bridge.so
   ```

5. 执行 `python -m robotsoul status` 验收。

## 8. 语音模型资源（首版准备）

语音资源由版本化 config/voice-resources.json 固定来源、版本、大小、SHA-256、目标架构和许可证来源。脚本不把模型或原生运行库放入 Git；开发机上的默认 Go2 目标缓存及安装目录为 build/linux-aarch64/voice/resources/。下载、安装前均验证锁定哈希；任一失败不会替换现有安装。当前 Xiao-Ya TTS 的模型卡声明其 BZNSYP/Data-Baker 数据许可仅限非商业用途；商业部署前须换成明确允许该用途的语音资源。

启用本地语音和网页通话时，先按目标 Python 版本安装项目的 `voice` extra（源码部署使用 `python -m pip install -e ".[voice]"`）。该 extra 提供 aiortc、PyAV、sounddevice 和 sherpa-onnx 所需 Python 依赖。资源脚本另行获取模型及匹配的 ARM64 原生运行包。当前清单中的 sherpa Python wheel 固定为 ARM64 CPython 3.11；目标机使用 CPython 3.11 时可从本地清单安装它：`python -m pip install --no-index --find-links build/linux-aarch64/voice/resources/wheels/linux-aarch64-cp311 sherpa_onnx==2.0.1`。wheel 只提供 Python 绑定，原生共享库仍按 `runtime/sherpa-onnx-aarch64` 中的发布包配置 `LD_LIBRARY_PATH`。离线资源包不包含 voice extra 的完整 Python 依赖 wheelhouse，离线安装时需另行准备与目标 Python/系统 ABI 匹配的依赖 wheelhouse。

离线部署时，开发机先运行 download，再运行 pack --root build/linux-aarch64/voice/resources --output voice-resources-linux-aarch64.zip；目标机运行 import --bundle voice-resources-linux-aarch64.zip、install 和 verify，然后使用上面的 `pip install --no-index` 安装 wheel。两端可用 --root 指定资源目录；重复传入 --id <resource-id> 可选择部分条目。离线导入仍以本地锁定清单比较 SHA-256，不信任包内替换的清单。模型和 ARM64 实机音频尚未在开发机验证，发布前必须完成目标板资源加载、扬声器路由、WebRTC/PyAV 和 10 小时长跑验收。

## 9. 常见错误

| 输出 | 检查方向 |
| --- | --- |
| `Set OPENROBOTSOUL_GO2_NETWORK_INTERFACE...` | 用 `ip -br link` 查连接机器人网络的接口并设置变量 |
| `Go2 SDK bridge library was not found` | 先成功运行 aarch64 构建；wheel 部署时设置 bridge 完整路径 |
| `timed out waiting for rt/sportmodestate` | 检查网卡选择、网络连通、防火墙和 DDS 网络；确认没有其他程序竞争 DDS 单例 |
| `client/server API versions do not match` | 核对机器人固件和 SDK revision，不要绕过版本检查 |
| `libddsc.so.0 => not found` | 检查 bridge 与 DDS `.so`/`.so.0` 是否同目录，并复查 `ldd` |
| `go.mod/go.sum contain local changes` | 恢复 PicoClaw submodule 到锁定状态后再构建 |

## 10. 动作能力上线前仍需完成

当前文档完成的是只读状态部署。若之后要让 Go2 执行动作，仍需针对实际固件验证并记录：单位与限值、动作完成反馈、超时停止、取消和断连行为、API 控制权与遥控器优先级、实体急停。完成这些之前，不要在 profile 中添加运动能力，也不要使用 `go2_low_level` 等 SDK 示例替代安全闭环。
