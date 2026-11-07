# App Pack 编写

App Pack 为一个应用提供领域词汇：把通用控件树（`group` / `text` / `button`）编译为带能力的命名实体（`PRODUCT p1 "Big Mac" +add`）。

App Pack 可选。没有 pack 时，运行时按通用模式编译窗口，仍可到达并操作控件；pack 补充通用路径无法推断的语义，例如哪个按钮表示加入购物车、行上的价格字段是什么。`--no-pack` 用于完全按通用模式驱动应用。

pack 可以写成单个 YAML 文件，也可以写成 Python 模块（内置 `restaurant-demo` 为 Python 模块）。`somnus.compiler.yaml_pack.load_pack_file` 解析单个文件并在当前进程注册；放在 `somnus/packs/*.pack.yaml` 下的 YAML 文件会在启动时加载。PyYAML 在 `pyproject.toml` 中声明；缺少 PyYAML 时内置 Python pack 仍可加载。CLI 的 `--pack` 只接受已注册的 pack id，不接受文件路径。

在 Python 中使用内置 pack：

```python
from somnus.compiler.pack_schema import get_pack
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session

session = Session(MockProvider(), pack=get_pack("restaurant-demo"))
```

## 1. 文件结构

```yaml
app:
  id: restaurant-demo            # 必需，唯一
  version: "0.2.0"
  match:
    process: "RestaurantDemo.exe" # 子串匹配，忽略大小写
    # window_class: "Notepad"      # 可选第二过滤条件
    # "*" 匹配任意进程（通用 pack 使用）

projection:                       # 有序；后面的规则优先
  - { select: "role=toolbar", rule: "collapse" }
  - { select: "name=导航", rule: "prune" }

named_refs:                       # 可选：命名锚点
  editor: { role: text, patterns: [value, text] }

entities:
  <entity-name>:
    match: <MatchRule>
    key: "$self.automation_id"    # 指纹主键
    attrs: { ... }
    caps: { ... }
```

## 2. 匹配

按进程匹配。窗口标题会随语言和打开的文档变化，使用标题会使 ref 失效。

`MatchRule` 字段：

| 字段 | 含义 |
| --- | --- |
| `role` | 通用角色，或列表（`[group, listitem]`） |
| `name` / `value` | 正则，或 `/regex/flags`（`i`、`s`、`m`） |
| `has_name` | `true` / `false` |
| `min_children` | 最小子节点数 |
| `patterns` | 必须全部存在的原生模式（`[value, text]`） |
| `has` | 必需的子节点；每项可带 `as: <member>` |

角色与模式使用 `compiler/normalize.py` / `providers/windows/uia.py` 的规范化值。UIA 的 `dataitem` 规范化为 `listitem`，`document` 规范化为 `text`。

### `has` 与成员

```yaml
match:
  role: [group, listitem]
  has:
    - { role: text, as: title }
    - { role: text, name: "/\\d+[.,]\\d{2}/", as: price_text }
    - { role: button, name: "/^(add|加入)$/i", as: add_btn }
```

每个 `as` 把第一个匹配的子节点绑定为成员名，可在 `key`、`attrs`、`caps` 中使用。

## 3. 属性表达式

`attrs` 的值可以是字面量、`$member.attr` 表达式，或带 `from` / `regex` / `cast` / `default` 的对象：

| 属性 | 含义 |
| --- | --- |
| `automation_id` / `id` | 原生 automation id |
| `name` | 节点名称 |
| `value` | 节点值 |
| `class_name` | 原生类名 |
| `role` | 通用角色 |

`from` 也接受 `descendant(role=text, name=/re/)`，从第一个匹配的子节点取值。

```yaml
attrs:
  name: "$title.name"
  price:
    from: "$price_text.name"
    regex: "(\\d+[.,]\\d{2})"
    cast: number          # number | int
    default: 0
```

## 4. 能力

```yaml
caps:
  add:
    via: ["$add_btn.activate"]     # 有序的原语步骤
    effects: ["cart.count += 1"]   # 可验证的后置条件
    risk: normal                   # safe | normal | critical
    params:
      size: { type: enum, enum: [small, medium, large] }
```

- `via` 指定成员与原语：`$self.activate`、`$add_btn.activate`、`$self.set_text`。原语集合固定为 `activate, focus, read, set_text, toggle, select, expand, collapse, increment, decrement, scroll, open, close`。`type_text` 与 `paste_text` 只作为能力内部的路径步骤存在，不作为模型可调用的能力。
- `effects` 供 verifier 判断动作是否生效。支持 `ref.field += N`、`-=`、`=`、`!=`、`contains`、`startswith`。运行时无法观察的字段视为不可验证，动作返回 `OK_UNVERIFIED`，不判失败。
- `risk: critical`（或 critical 能力名）在 SUI-Text 中打印为 `!`。`!` 是描述性元数据；当前构建没有确认门控，能力被调用时直接执行。

`key` 是指纹主键，优先使用 `$self.automation_id` 或 pack 成员的 automation id。`key` 可解析时，易变名称从指纹中排除，变化的标签不会使 ref 失效。

### 条件步骤与合成输入回退

`via` 的一项可以是映射或字符串，用目标暴露的原生模式门控该步骤。条件在编译期求值，解析器只会收到目标实际可执行的步骤，模型不会看到回退原语。

```yaml
caps:
  set_text:
    params: { text: { type: string, required: true } }
    via:
      # 优先使用原生 Value 模式
      - { target: "$self", primitive: set_text, value: "$text", when_has: value }
      # 其次使用剪贴板通道（--allow-clipboard）
      - { target: "$self", primitive: paste_text, value: "$text", when_missing: value }
      # 最后聚焦并键入（声明式键盘回退）
      - { target: "$self", primitive: type_text, value: "$text", when_missing: value }
```

`when_has` / `when_missing` 指定原生模式（`value`、`invoke`、`text` 等）。编译后每个目标只保留其中一个步骤，同一 pack 因此可以同时用于原生编辑控件与只接受键盘输入的自绘控件。

内置 `set_text` 链按运行时尝试顺序排列：`set_text`（Value 模式）→ `paste_text`（剪贴板）→ `type_text`（键盘）。三条路径都会先提升目标窗口；后两条产生合成输入，剪贴板路径还会借用用户剪贴板。完整模型见 [text-input-channels.md](text-input-channels.md)。

## 5. 为新应用编写 pack

pack 可选。没有 pack 时编译器运行通用模式，仍会规范化角色、从原生模式推导能力、构建关系并投影树。只要控件暴露标准 UIA 模式，应用打开后即可操作。

pack 提供的是确定性，不增加可达性：

| 项 | 通用模式 | App 模式 |
| --- | --- | --- |
| 蓝牙开关 | `BUTTON b10 "蓝牙" +toggle` | `TOGGLE_SETTING b10 "蓝牙" +toggle` |
| agent 需要做的事 | 读取名称并推断 | 类型直接说明 |
| 匹配依据 | 名称 + 原生模式 | `class_name` / `automation_id`，与语言无关 |
| 领域动作 | 原语集合 | 语义能力（`add`、`submit` …） |

通用能力只从模式推导，不做猜测；没有暴露模式的控件没有能力。

### 编写时机

- 应用包含可命名的领域对象（`product`、`invoice`、`file`）。
- 同一控件类型需要按类区分（WinUI `ToggleSwitch` 与普通 `Button`）。
- 一个能力需要多个步骤或效果验证。
- 动作在语义上重要，需要标记 `risk: critical`；当前构建打印 `!` 并直接执行。

### 步骤

1. 录制应用树：

   ```bash
   somnus windows
   somnus --provider uia record w1 -o app.json
   ```

2. 检查树，寻找稳定且与语言无关的锚点，优先使用 `patterns`、`role` 与 `automation_id`：

   ```python
   from somnus.providers.mock import load_tree
   tree = load_tree("app.json")
   for n in tree.walk():
       print(n.role, n.native.automation_id, n.name, sorted(n.patterns))
   ```

3. 编写 pack，并在无桌面环境中检查。pack 必须先注册：调用 `load_pack_file("my-app.pack.yaml")`，或把文件放在 `somnus/packs/` 下由启动加载。`--pack` 只接受注册后的 id。

   ```bash
   # pack 已通过 load_pack_file 注册，或已放入 somnus/packs/
   somnus --provider mock --fixture app.json --pack my-app observe
   ```

4. 在 `tests/` 下保留裁剪后的固件，并在测试中断言实体。示例见 `tests/compiler/test_yaml_pack.py`。

## 6. UWP 窗口（ApplicationFrameHost）

UWP 应用在树中表现为进程为 `ApplicationFrameHost.exe` 的 `ApplicationFrameWindow`；应用自身的进程位于框架的子节点中。两项实现使其可用：

1. 发现：桌面根使用 `ControlViewWalker` 遍历，可发现 UWP 框架；对根子节点使用 ControlType 过滤则不会。Win32 `EnumWindows` 补齐遍历器未到达的窗口。
2. 身份：窗口进程为 `ApplicationFrameHost.exe` 时，provider 遍历该框架的直接子节点，找到 pid 不同的子节点，该子节点属于真实应用。窗口的 `process` 随后报告为例如 `SystemSettings.exe`，因此 `match: { process: systemsettings.exe }` 可用。

两项行为由 `tests/e2e/test_windows_windows.py` 覆盖。
