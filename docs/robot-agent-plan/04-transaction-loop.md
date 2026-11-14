# 事务处理流与数据回环

## 目标

事务流负责把“任务目标”转为可追踪、可停止、可评估的动作过程。数据回环则持续处理感知与执行反馈，更新当前状态，为 Agent 提供最新而非过期的决策依据。

## 任务状态机

~~~mermaid
stateDiagram-v2
  [*] --> received
  received --> contextualizing
  contextualizing --> awaiting_agent
  awaiting_agent --> validating_intent
  validating_intent --> executing
  validating_intent --> needs_confirmation
  validating_intent --> failed
  needs_confirmation --> executing
  needs_confirmation --> cancelled
  executing --> monitoring
  monitoring --> evaluating
  monitoring --> recovering
  monitoring --> cancelled
  recovering --> awaiting_agent
  recovering --> failed
  recovering --> cancelled
  evaluating --> completed
  evaluating --> awaiting_agent
  evaluating --> failed
  completed --> [*]
  failed --> [*]
  cancelled --> [*]
~~~

具体状态可按 MVP 简化，但终态必须清晰。事务是逻辑工作流，不意味着真实机器人动作能被数据库回滚。

另有一个与任务状态并行的交互状态机，用来决定何时形成可交给 Agent 的任务输入：

~~~mermaid
stateDiagram-v2
  [*] --> idle
  idle --> trigger_candidate: wake / gesture / attention event
  trigger_candidate --> listening: policy accepts trigger
  trigger_candidate --> idle: low confidence / expired
  listening --> transcript_ready: speech detected
  listening --> idle: timeout / cancelled
  transcript_ready --> task_created: transcript valid
  transcript_ready --> clarification: low confidence / ambiguous
  clarification --> listening: user responds
  clarification --> idle: timeout / cancelled
  task_created --> idle: task handed to task state machine
~~~

“attention”仅是可选触发信号。若策略配置为多信号联合触发（例如视觉注意力加唤醒词），事务流需在有界时间窗内关联同一交互上下文；不能把注视摄像头当作身份认证。

## 正常处理步骤

1. 交互回环等待已授权的唤醒词/手势/可选注意力信号；按配置开启有限时长的语音采集。最终转写有效后，与 Dashboard 文本一样作为自然语言创建 task_id，并记录机器人、时间和触发来源，交给常驻对话进程 `openrobotsoul-dialogue`（链接 `third_party/picoclaw` AgentLoop）规划。不按本地关键词表选择技能；编码用的 Go 版 PicoClaw CLI 不参与这次技能选择。
2. 订阅新观测并检查有效期、来源和权限。状态估计更新时保留证据与置信度。
3. 将相关状态、技能清单和任务进度整理为 Agent 上下文。
4. Agent 返回意图或工具调用；回环验证技能可用性、schema、数值边界、确认要求和任务状态。
5. 经本地安全监督后提交命令，记录 command_id 并接收动作生命周期反馈。
6. 在动作执行期间并行处理传感器事件与本地快速控制结果；危险或急停可取消/覆盖当前高层动作。
7. 将反馈融合进状态，判断前置条件和目标条件。根据评估结果完成、请求下一步、有限恢复或结束任务。
8. 向用户返回结论及必要的证据摘要，并持久化最小必要的任务审计记录。

超时、没有后续语音、低置信度转写、多人同时发言、用户取消或触发事件过期时，关闭当前采集窗口并回到 idle，或在适当情况下询问澄清；不得因一次手势候选直接调用运动技能。

## 数据融合规则

不能把原始观测、模型意图和执行状态混成一个文本字段。建议维护分离的事实记录：

- observation：设备上报的证据，保留来源、时间和有效期。
- estimated_state：事务流基于有效证据计算的当前估计。
- agent_intent：云端模型建议采取的高层步骤及理由摘要。
- command/feedback：实际发出的命令及控制器返回的生命周期。
- evaluation：目标是否达成、依据、置信度和遗留问题。

状态合并需定义事件时间排序、迟到事件、重复事件和冲突来源处理。过期的主人位置或手势事件不能继续作为当前事实使用。

## 并发快速反应

高速运动控制持续在本地控制器运行，使用传感器的实时输入形成自己的闭环。Agent 规划环是低频、可中断的语义闭环。事务流应能接收本地安全事件并使当前任务暂停、取消或进入恢复状态；不应让高频数据逐帧上传云端等待 LLM。

空闲主动行为由本地资格策略触发成有明确来源的任务（例如 proactive.greeting 或 proactive.explore），并复用同一任务状态机、动作适配器和评估流程。资格必须在启动前和执行中持续检查；云端 Agent 不负责自我授予探索权限。

主动状态可包括已知地图版本/变化、经授权的熟人条目及最近交互、电量/预计返航余量、充电点可达性和当前 savepower 状态。地图更新和熟人记忆作为带来源与置信度的候选记录，不能把未验证的地图变化或陌生人自动升级成事实/朋友。

低电量应触发能量管理任务：若返航余量充分，优先尝试已验证的返回充电点/充电流程；若无法自主充电，则寻找允许的求助渠道并向用户报告；若通信或移动不安全，则停止非必要感知/动作、保存必要状态并进入本地 savepower。阈值和转换必须由 profile 与本地策略确定。

## 结果评估与恢复

每个技能声明可观察的完成条件和失败类别。任务成功必须由可用的机器人/传感器反馈或明确的用户确认支持，不能只凭 API 返回成功文本。评估结果包括目标、结论、依据、时间和置信度。

恢复按错误类型配置：可恢复设备错误可有限重试；危险、状态不确定或非幂等动作失败时先停止并查询真实状态；需要用户选择或授权时暂停询问。所有自动重试必须有次数上限、截止时间和幂等规则。

## 可追踪性

任务输入、感知事件、模型轮次、工具调用、动作反馈和最终评估都携带 task_id；跨服务时加入 trace_id 和关联 ID。状态变化记录前后状态、原因、来源及时间，避免只保存最终摘要而无法诊断。
