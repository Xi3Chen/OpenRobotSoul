# 语音简短指令重构方案

## 现状与问题

`robotsoul/voice/commands.py` 将中文正则表达式、动作逻辑和物理常量全部硬编码在 voice 模块中。
这导致：

- **语言绑定**：换语言必须改核心模块
- **动作绑定**：Unitree 专属动作（`sport.stand_up` 等）对其他设备毫无意义，却出现在公共代码里
- **扩展困难**：新增设备要么修改公共文件，要么无处声明自己的短语

根本原因：**动作和它的简短指令是同一件事的两面**，应由声明动作的地方一并声明，而不是分散到另一个模块。

---

## 设计目标

1. `voice/` 模块不包含任何具体动作或语言相关内容
2. 设备适配器声明自己支持哪些短语，以及如何提取参数
3. 简单命令（无参数）通过配置文件维护，无需写代码
4. 参数型命令（后退 X 米、点 N 下头）通过适配器注册命名提取器解决
5. 参数范围收紧（clamp）由 registry 统一负责，适配器不介入
6. 组合命令（多 skill 编排）由适配器代码声明，不进配置文件
7. 多语言支持 = 新增一个配置文件，不改任何 Python
8. 简短指令功能可在运行时开关，避免与自然交互冲突

---

## 跨设备扩展策略

**不提供通用基类或可继承的命令配置**。原因：

- 不同形态机器人的动作能力差异巨大（四足 vs 轮式 vs 人形）
- 强行抽象会带来过度设计和不必要的约束
- 实际开发中，开发者需要完全控制自己设备的命令映射

**采用模板参考方式**：

- `unitree/` 和 `mimax2/` 作为两个形态的参考实现
- 新设备开发者复制最接近的模板适配器目录
- 根据自己设备的能力修改配置文件和提取器

这类似于选择脚手架：拿到可运行的代码后按需裁剪，比从零理解抽象接口更直接。

---

## 架构变更

### 1. `voice/command_registry.py`（替换 `commands.py`）

纯匹配引擎，不知道任何具体动作：

```python
class CommandRegistry:
    def register_simple(
        self,
        skill_name: str,
        patterns: list[re.Pattern],
    ) -> None: ...

    def register_parametric(
        self,
        skill_name: str,
        pattern: re.Pattern,
        extractor: str,
        extractor_kwargs: dict = {},
    ) -> None: ...

    def register_compound(
        self,
        pattern: re.Pattern,
        steps: list[str],       # 有序 skill 名列表
        label: str = "",
    ) -> None: ...

    def register_extractor(
        self,
        name: str,
        fn: Callable[[re.Match, Mapping[str, Any]], dict],
    ) -> None: ...

    def load_config(self, path: Path) -> None: ...

    def match(
        self,
        text: str,
        capabilities: Mapping[str, Any],
        *,
        wake_phrase: str | Sequence[str] = "",
    ) -> AgentIntent | AgentPlan | None: ...
```

**clamp 职责**：`match()` 在返回 `AgentIntent` 之前，自动按 `capability_param` 从 `capabilities` 读取声明的 `minimum`/`maximum`，对提取到的参数值做 clamp。适配器和提取器函数均不负责这一步。

**快速反应控制**：
- `enabled=False`：完全禁用简短指令匹配，所有语音都进入对话流
- 用途：让用户可以关闭"条件反射式服从"，保留自然交互。关闭后，即使说"站起来"也会进入对话流，由 AI 根据情境（心情、状态、关系）决定是否执行、何时执行、或如何回应

**内置提取器**（由 `command_registry.py` 自动注册，无需适配器声明）：

| 提取器名称 | 作用 | 配置字段 |
|---|---|---|
| `integer_count` | 提取中文/阿拉伯数字计数 | `default`（缺省值） |
| `distance_m` | 提取距离（支持米/步/无单位） | `default_m`、`step_m` |

---

### 2. 配置文件格式（YAML）

配置文件紧贴适配器包存放，每个适配器可携带多个 locale 文件，命名为 `commands.<locale>.yaml`：

```
unitree/
  adapter.py
  commands.zh.yaml      ← 中文简短指令
  commands.en.yaml      ← 英文（未来）
```

配置文件只声明**简单命令**和**参数型命令**。
组合命令不在配置中——见 [§组合命令]。

**`commands.zh.yaml` 示例：**

```yaml
version: 1

# 简单命令：pattern 匹配即触发，无参数提取。
# 每个 skill 下可列多条 pattern，任一匹配即触发。
simple:
  sport.stand_up:
    - "^(?:请)?(?:你)?(?:站起来|起立|站立|起来)$"
  sport.stand_down:
    - "^(?:请)?(?:你)?(?:趴下|卧倒)$"
  motion.turn_around:
    - "^(?:请)?(?:你)?(?:向后转|往后转|掉头|转过身|转身|(?:向后|往后)?转(?:一)?(?:百)?八十度)$"
  motion.turn_left:
    - "^(?:请)?(?:你)?(?:向左(?:边|面)?转|往左(?:边|面)?转|左转)$"
  motion.turn_right:
    - "^(?:请)?(?:你)?(?:向右(?:边|面)?转|往右(?:边|面)?转|右转)$"

# 参数型命令：引用内置或自定义提取器。
# registry 负责按 capability_param 自动 clamp 到设备声明的范围。
parametric:
  expression.nod:
    pattern: "^(?:请)?(?:你)?点(?:(?P<count>[0-9]+|[一二两三四五]))?(?:下|个|次)?头$"
    extractor: integer_count
    default: 1
    capability_param: count

  expression.shake_head:
    pattern: "^(?:请)?(?:你)?摇(?:(?P<count>[0-9]+|[一二两三四五]))?(?:下|个|次)?头$"
    extractor: integer_count
    default: 1
    capability_param: count

  expression.tilt_head:
    pattern: "^(?:请)?(?:你)?(?:向|往)?(?P<direction>左|右)?(?:边|面)?歪(?:一)?(?:下|个)?头$"
    extractor: tilt_head_direction   # 自定义提取器，由适配器注册
    capability_param: direction

  motion.back_up:
    pattern: >-
      ^(?:请)?(?:你)?(?:后退|往后退|向后退|退后)
      (?:(?P<count>[0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十])(?P<unit>米|[mM]|步))?$
    extractor: distance_m
    default_m: 0.5
    step_m: 0.3
    capability_param: distance_m

  # 侧移：通过 skill_selector 按命名组分发到不同 skill
  motion.strafe:
    pattern: >-
      ^(?:请)?(?:你)?(?:向|往)(?P<direction>左|右)(?:边|面)?(?:侧移|平移|横移)
      (?:(?P<count>[0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十])(?P<unit>米|[mM]|步))?$
    extractor: distance_m
    default_m: 0.5
    step_m: 0.3
    capability_param: distance_m
    skill_selector:
      group: direction
      mapping:
        "左": motion.strafe_left
        "右": motion.strafe_right
```

> `skill_selector` 为可选项，用于一条 pattern 根据命名组的值分发到不同 skill。
> 无 `skill_selector` 时，pattern 直接绑定到声明的 skill 名。

---

### 3. 组合命令

组合命令由适配器代码注册，不进配置文件。原因：组合命令往往携带设备特有的前置条件判断逻辑，放进配置会引入类似"用 YAML 写程序"的问题。

```python
# unitree/adapter.py
class UnitreeAdapter:
    def setup_voice_commands(self, registry: CommandRegistry, locale: str = "zh") -> None:
        # 1. 加载配置文件（简单命令 + 参数型命令）
        config_path = Path(__file__).parent / f"commands.{locale}.yaml"
        registry.load_config(config_path)

        # 2. 注册自定义提取器
        registry.register_extractor("tilt_head_direction", _extract_tilt_direction)
        registry.register_extractor("move", _extract_move)   # 见下文

        # 3. 注册参数型命令（参数结构复杂，配置文件表达力不足）
        registry.register_parametric(
            "sport.move",
            re.compile(
                r"^(?:请)?(?:你)?(?:向|往)?(?P<direction>前|后)(?:边|面)?(?:走|移动)"
                r"(?:(?P<count>[0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十])"
                r"(?P<unit>步|秒|分钟|分|米|[mM]))?$"
            ),
            extractor="move",
        )

        # 4. 注册组合命令
        registry.register_compound(
            re.compile(
                r"^(?:请)?(?:你)?(?:站起来|起立|站立|起来)"
                r"(?:然后|再|，|,|、)?"
                r"(?:向后转|往后转|掉头|转过身|转身|(?:向后|往后)?转(?:一)?(?:百)?八十度)$"
            ),
            steps=["sport.stand_up", "motion.turn_around"],
            label="闭集：站起来再向后转",
        )
```

**`sport.move` 单独注册而不进配置**，因为它同时存在两种调度路径（距离 → `sport.move_distance`，时间/步 → `sport.move`），参数 dict 结构不同，单一提取器无法通过配置字段表达，需要代码。

---

### 4. 物理常量的归属

| 原常量 | 新归属 |
|---|---|
| `_STEP_M`（步长 fallback） | 配置文件 `step_m` 字段，每个动作独立声明 |
| `_DEFAULT_TRANSLATE_M` | 配置文件 `default_m` 字段 |
| `_WALK_SPEED`（行走速度） | 设备 capability spec 的 `vx` 范围；registry clamp 时读取 |

---

## AI 学习用户口癖

用户可能有个性化的表达习惯（"狗子站好" vs "站起来"、方言、口音变体）。支持 AI 动态学习并添加新映射。

### 三层配置优先级

```
用户层 (commands.user.yaml)         ← AI 学习 + 用户手动编辑
     ↓ 覆盖
厂商层 (commands.zh.yaml)          ← 设备默认配置（模板）
```

### 学习流程

1. **用户说出未匹配的指令**："狗子给我站好"
2. **Registry 匹配失败**：返回 `None`
3. **回退到 LLM 语义理解**（在 `transcript.py` 调用）：
   ```python
   # 伪代码
   llm_result = dialogue_context.understand_as_skill(
       text="狗子给我站好",
       available_skills=capabilities.keys()
   )
   # 返回: {"skill": "sport.stand_up", "confidence": 0.92}
   ```
4. **高置信度时询问确认**：机器人问"你是想让我'站起来'吗？"
5. **用户确认后持久化**：
   ```python
   registry.add_user_pattern("sport.stand_up", "^狗子给我站好$")
   registry.save_user_config("commands.user.yaml")
   ```

### 配置文件格式

```yaml
# commands.user.yaml
version: 1

simple:
  sport.stand_up:
    - "^狗子给我站好$"        # [learned: 2026-11-17]
    - "^站好$"               # [learned: 2026-11-15]

# 用户层只应包含简单的 pattern 映射
# 复杂参数型命令不通过学习添加（需厂商在代码中定义）
```

### Python 代码 vs 配置文件的权衡

**当前方案**：
- **YAML 配置**：简单命令（80%）和常规参数命令（计数、距离、方向枚举）
- **Python 代码**：复杂逻辑命令（如 `sport.move` 的双路径调度、组合命令的前置条件判断）
- **AI 学习**：只写入 `commands.user.yaml`，不生成或修改 Python 代码

**理由**：
- 配置文件多了确实增加维护成本，但 AI 修改配置比生成代码更安全
- 复杂命令放在 Python 中保持灵活性，避免"用 YAML 写程序"
- 用户学习层完全隔离，不污染厂商模板

### Registry API 扩展

```python
class CommandRegistry:
    def add_user_pattern(
        self,
        skill_name: str,
        pattern: str | re.Pattern,
        metadata: dict = {},
    ) -> None:
        """运行时添加用户个性化命令"""
        
    def save_user_config(self, path: Path) -> None:
        """持久化用户学习的映射到 commands.user.yaml"""
        
    def load_user_config(self, path: Path) -> None:
        """加载用户配置（优先级高于厂商配置）"""
```

### 使用示例

```python
# 适配器初始化时
registry.load_config(Path(__file__).parent / "commands.zh.yaml")  # 厂商
registry.load_user_config(user_data_dir / "commands.user.yaml")   # 用户（可选）

# voice pipeline 中
intent = registry.match(text, capabilities, enabled=True, priority=0.8)
if not intent and learning_enabled:
    # 回退到 LLM 语义理解
    llm_intent = await dialogue_context.understand_as_skill(text, capabilities.keys())
    if llm_intent.confidence > 0.85:
        confirmed = await ask_user_confirm(llm_intent.skill)
        if confirmed:
            registry.add_user_pattern(llm_intent.skill, text)
            registry.save_user_config(user_config_path)
            intent = AgentIntent(skill=llm_intent.skill, params={})
```

---

## 快速反应功能的开关设计

简短指令是"条件反射式"的快速响应机制。但这与项目初衷可能冲突：

- **问题**：百分百服从指令会降低自然感和情绪价值
- **需求**：用户应该能关闭或调节这个功能，让机器人有自己的"个性"

### 控制参数

在设备 capability 或全局配置中声明：

```yaml
voice:
  closed_set_enabled: true      # 是否启用简短指令功能
  learning_enabled: true        # 是否允许 AI 学习新口癖
  learning_confirmation: true   # 学习前是否需要用户确认
```

### 行为说明

| `closed_set_enabled` | 行为 |
|---|---|
| `false` | 所有语音都进入对话流，不做 closed-set 匹配。AI 根据情境决定是否执行动作 |
| `true` | 匹配到简短指令时立即执行（条件反射式） |

**设计理念**：

- **开启时**：快速响应，适合工具型机器人或需要精确控制的场景
- **关闭时**：所有语音交给 AI 对话系统处理，AI 根据当前状态（心情、疲劳度、与用户的关系）决定如何回应
  - 可能立即执行
  - 可能先撒娇再执行
  - 可能假装没听见
  - 可能拒绝并给出理由
- 这保留了宠物式交互的情绪价值——养宠物不是要一个百分百遵从指令的机器

### 实现位置

```python
# voice pipeline 调用示例
if settings.voice.closed_set_enabled:
    intent = registry.match(text=transcript, capabilities=capabilities)
    if intent:
        # 立即执行简短指令
        await execute_intent(intent)
        return

# 未匹配或功能关闭，进入对话流
# AI 在这里根据情境（心情、状态、关系）决定如何响应
response = await dialogue_context.process(transcript)
```

---

## 文件结构变化

```
robotsoul/voice/
  command_registry.py     ← 原 commands.py，改为纯引擎（无具体动作）
  transcript.py           ← 不变

unitree/                  ← 或现有适配器目录
  adapter.py              ← setup_voice_commands() 注册所有命令
  commands.zh.yaml        ← 简单 + 参数型命令，中文
  commands.en.yaml        ← 英文（后续按需添加）
  
用户数据目录/
  commands.user.yaml      ← AI 学习的个性化映射（可选）
```

---

## 迁移步骤

1. 新建 `voice/command_registry.py`，实现 `CommandRegistry`、`load_config()` 和两个内置提取器（`integer_count`、`distance_m`）
2. 将 `commands.zh.yaml` 按上述格式填写（从现有正则搬运）
3. 适配器实现 `setup_voice_commands()`，注册自定义提取器、复杂参数命令和组合命令
4. voice pipeline 初始化时调用 `adapter.setup_voice_commands(registry)`，将原来的 `match_closed_command(text, capabilities)` 调用替换为 `registry.match(text, capabilities)`
5. 确认行为一致后删除 `commands.py`

迁移期间两套可以并存，以特性开关控制，逐步验证。
