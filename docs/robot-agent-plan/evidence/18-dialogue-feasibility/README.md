# PicoClaw 对话接入验证附件

关联报告：[Agent 运行底座可行性评估](../../19-agent-foundation-feasibility.md)。

本附件是可行性研究的最小探针，不是生产对话进程或新增业务实现。

- [probe_test.go](probe_test.go)：6 个探针，含正常接入、已知缺陷复现和局部修正验证。
- [probe-summary.txt](probe-summary.txt)：环境、命令、结论和限制。
- [probe-output.txt](probe-output.txt)：当次运行的原始输出。

全部模型响应来自假 provider，所有含 SECRET、FAKE、TURN、NOTE 的字符串都是人工构造的哨兵，不是真实用户数据或凭证。探针使用临时工作区、独立 PICOCLAW_HOME 与 builtin skills 目录；不调用机器人或手机。

## 如何解读结果

6 个测试 PASS 表示观察到了各自规定的行为。其中 `TestBareKeysCollide` 和 `TestSummaryAppendLossRepro` 故意断言当前版本存在问题，不能作为缺陷已经修复的证明。上游修复后，应将这些缺陷探针改为相应的正确行为断言。

`TestUnexpectedToolCall` 主要记录返回值与历史，用于观察错误路径。`TestNoToolsFrontmatterAndProviderGuard` 则断言受控空工具配置及 provider 拦截有效。对话恢复只测存储关闭后重开，不代表常驻进程崩溃恢复已通过。

## 复现

依赖：Go 不低于上游 go.mod 要求的 1.25.13，仓库 PicoClaw checkout 为 `bbf6893ca7afad27f1d00a0f5a45982a549c6ed6`。本次使用 Go 1.27.3 / Windows amd64。

在一个新建的仓库外临时目录中复制 `probe_test.go`，创建下面的 `go.mod`，把 replace 目标替换成自己的实际绝对路径：

```go
module example.com/dialogueprobe

go 1.25.13

require github.com/sipeed/picoclaw v0.0.0

replace github.com/sipeed/picoclaw => C:/Users/Xi3Chen/Documents/MyProject/RobotHarness/third_party/picoclaw
```

在该临时目录执行：

```text
go test -mod=mod -v -count=1 -timeout=90s .
```

依赖解析可能访问 Go 模块镜像；运行本身没有真实模型或设备请求。不要在子模块里执行探针，也不要把临时目录生成的 go.mod/go.sum 覆盖到子模块。

当次仅使用上述 PicoClaw replace 即编译成功。上游依赖模块中的其他 replace 不会自动传递，换平台或扩展导入时仍需重新验证。
