# 统一跨平台构建工具

## 命令

```powershell
python -m robotsoul.build --platform linux --arch x86_64
python -m robotsoul.build --list-targets
python -m robotsoul.build
```

也可以安装项目后使用 `robotsoul-build` 命令。传入 `--platform` 和 `--arch` 时使用非交互模式，适合 CI；完全不传参数时进入向导，选择系统、架构和构建类型，并显示依赖诊断。`--guide` 只打印安装指引，不运行 CMake。

## 输出布局

Go2 C++ 示例、只读状态 bridge 和 PicoClaw 会由一次构建命令统一构建。所有构建内容都位于项目根目录 `build/`：

```text
build/
  linux-x86_64/
    cmake/       # CMake cache、编译器中间文件和原始构建输出
    bridge/      # Go2 SDK bridge 的 CMake 构建目录
    go/          # Go build cache、Go module cache 和中间文件
    bin/         # Go2 示例和 PicoClaw 可执行文件
    lib/         # bridge、Unitree 和 DDS 运行库副本
    manifest.json
```

`manifest.json` 记录目标平台、架构、构建类型、二进制路径、依赖路径和 toolchain。Go 编译时设置 `GOCACHE` 和 `GOMODCACHE` 到该 target 的 `go/` 目录，并使用 `-mod=readonly`；Go 不会改写 PicoClaw 子模块里的依赖清单。`build/` 已加入 Git 忽略规则。

## 平台和架构

工具接受以下别名：

- 系统：`linux`、`windows`、`macos`（也识别 `ubuntu`、`win32`、`darwin` 等常见写法）
- 架构：`x86_64`/`amd64`/`x64`、`aarch64`/`arm64`

目标和宿主不同且没有 `--toolchain` 时直接拒绝，避免使用宿主编译器生成错误架构。构建器还会检查 `cmake`、C++ 编译器和 Go；Go 必须为 1.25.13 或更新版本（与 PicoClaw `go.mod` 一致），缺少或版本过低时直接报错并说明理由。若 `go.mod` 或 `go.sum` 在 PicoClaw 子模块中有本地改动，也会在编译前拒绝，避免构建期间改写依赖锁定文件。

## SDK 限制

当前 Unitree SDK checkout 的 CMake 工程直接导入 `lib/<arch>/libunitree_sdk2.a`、`thirdparty/lib/<arch>/libddsc.so` 和 `libddscxx.so`，预编译库只适用于 Linux `x86_64`/`aarch64`。构建工具会列出这些实际可用目标；Windows/macOS 或缺失架构必须先放入对应 SDK/依赖并调整 CMake 工程，工具不会假装完成构建。

构建出的二进制包括 Go2 官方高层服务、视频、VUI 示例、`picoclaw` CLI 和只读 `libopenrobotsoul_go2_bridge.so`。运行 Go2 只读状态检查还需设置 `OPENROBOTSOUL_GO2_NETWORK_INTERFACE` 为连接机器人网络的本机网卡名；Python coding-agent adapter 默认从 `build/<platform>-<arch>/bin/` 加载 PicoClaw，仍可通过 `OPENROBOTSOUL_PICOCLAW_BINARY` 覆盖。bridge 只读 DDS 运动状态，不包含运动 API；官方示例和 bridge 的编译成功不代表实机动作经过验证。
