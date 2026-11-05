# Agent 选型与 PicoClaw 集成决策

## 决策状态

阶段性选型：先用 PicoClaw 替代 Pi coding agent，用于隔离工作区中的插件/补丁生成与代码任务评估。它不替代机器人运行时 Agent；后者仍需独立选型。当前 Python MVP 的本地规则 Agent 保持用于模拟和接口验证。

自动部署、canary 和回滚仍受阶段 8/9 的验收门禁约束。本决策不授予 PicoClaw 访问机器人设备、生产凭证、用户记忆或运行时主目录的权限。

## 集成形态

项目宿主以 Python 为主，PicoClaw 为 Go 程序；上游通过 Git 子模块固定在 `third_party/picoclaw`，当前固定提交记录于主仓库的 submodule gitlink。构建目标平台对应的独立可执行文件，由 Python 宿主负责启动、监控、超时和关闭。PicoClaw 是源码依赖，不是 Python 包；Python 进程不会直接导入 Go 源码。

Python 宿主通过 `picoclaw agent --message ... --session ...` 启动单次 coding task 子进程；每个任务有唯一的 task/session ID、独立工作目录和硬超时，宿主记录退出码、标准输出、错误输出及超时状态。PicoClaw stdout 是人类可读文本而非 JSON API；结构化结果由 Python 宿主封装。上游 Gateway HTTP 面向聊天渠道集成，不用于本地任务桥接。

PicoClaw 文件工具限制在指定工作区，Shell、MCP、web、后台任务和非必要工具默认关闭。Python 宿主以最小环境启动 PicoClaw，将用户 config/secret 复制到任务临时 home 并在进程结束后清理；HOME、TEMP 等用户目录也重定向到该临时目录。若 PicoClaw hooks 或自进化配置启用则拒绝启动。工作区必须位于项目仓库之外，通常使用单独 Git worktree。工作区路径检查与 PicoClaw 文件工具限制不构成 OS 沙箱；shell 仅可在容器/虚拟机等效隔离已落实后通过显式参数开启。当前集成依赖配置文件中的 API key；基于用户目录的 OAuth 登录凭证暂不继承。任何生成结果均须经静态检查、依赖/许可检查、契约测试和风险门禁。

## 验收条件

- 固定 PicoClaw 上游提交和构建方式，在项目支持的目标平台可重复构建。
- Python 宿主能启动、监控、超时和关闭子进程；故障、取消及退出码可追踪到任务 ID。
- 子进程使用独立临时 home/config/session；主机环境变量和 API 密钥不直接继承，任务结束后临时凭证副本被清理。
- Agent 能在临时工作区完成代表性文件读取、编辑、测试和补丁输出任务。
- 越出工作区的文件访问、未声明网络和未经允许的命令会被隔离层拒绝。
- 工具调用与结果可结构化关联，不能把工具受理当作代码验证通过或部署成功。
- 生成变更继续经过插件风险分类、授权、canary 和回滚流程。

验收完成前，PicoClaw 用于离线/隔离评估，不接入自动部署路径。上游 README 提示项目仍处快速开发阶段，并建议 v1.0 前不要用于生产部署；因此需要固定提交并在升级时重新执行兼容性、安全和回归评估。

## 上游来源

- 项目：<https://github.com/sipeed/picoclaw>
- 固定提交：`bbf6893ca7afad27f1d00a0f5a45982a549c6ed6`
- 上游许可证：MIT；更新版本时仍需复核许可证、依赖和构建产物。
