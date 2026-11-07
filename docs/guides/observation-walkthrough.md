# 观察示例：一个窗口与四次工具调用

本文以一个真实的 Windows 属性对话框为例，展示模型收到的 SUI-Text。

完整工具面为九个：`observe`、`find`、`inspect`、`call`、`run`、`help`、`switch`、`launch`、`configure`。默认 MCP 宿主暴露六个 core 工具。`switch` 用于在 Windows 会话之间移动 agent，见 [../reference/child-session.md](../reference/child-session.md)。

## 1. `observe`

`observe` 返回目标窗口的内容，并以一行 `WINDOWS` 给出桌面上下文。其他窗口的内容不展开；只需要窗口清单时使用 `OBSERVE windows`。

```
@rev 1
WINDOWS w1 "agent 属性" active
WINDOW w1 "agent 属性" +scroll_into_view
  SCREEN s1 "网络文件和文件夹共享"
  IMAGE im1 "网络文件和文件夹共享"
  TEXT tx1 "agent"
  TEXT tx2 "不共享"
  TEXT tx3 "网络路径(N):"
  INPUT q1 "网络路径(N):" +set_text(text)
  BUTTON b1 "共享(S)..."
  SCREEN s2 "高级共享"
  TEXT tx4 "设置自定义权限，创建多个共享，并设置其他高级共享选项。"
  BUTTON b2 "高级共享(D)..."
  SCREEN s3 "密码保护"
  SCREEN s4 "用户必须具有此计算机的用户帐户和密码，才能访问共享文件夹。…"
    LINK lnk1 "网络和共享中心"
  BUTTON b3 "确定"
  BUTTON b4 "取消"
  BUTTON b5 "应用(A)" disabled
  TAB t1
    TAB t2 "常规"
    TAB t3 "共享"
    TAB t4 "安全"
    TAB t5 "以前的版本"
    TAB t6 "自定义"
  PANE pn1
    MENU mn1 "系统"
      MENUITEM mi1 "系统" +collapse +expand
    BUTTON b6 "关闭"
```

从这次观察可以直接得到的信息：

| 信息 | 来源 |
| --- | --- |
| 界面已就绪、目标窗口在位 | `WINDOW` 行与 `+scroll_into_view` |
| 界面的语义分区 | `SCREEN s1 "网络文件和文件夹共享"` 等 |
| 哪些是输入框 | `INPUT q1`；`+set_text(text)` 表示可写，只读字段显示 `readonly` 且没有 `+set_text` |
| 哪些可以点击 | `BUTTON b1 "共享(S)..."` |
| 当前不可用的按钮 | `BUTTON b5 "应用(A)" disabled` |
| 可用的操作 | 行内 `+set_text(text)`、`+select`；`activate` 按角色隐含 |
| 折叠的集合 | 本窗口的 tab 全部展开，没有折叠 |
| 长文本被截断 | `…` 表示超过 80 字符被裁剪 |

以下信息不出现在模型输入中：控件句柄、坐标、`AutomationId`、进程 ID、COM 指针、原始 UIA `control_type` 字符串、无名的 `Pane` 包装节点。

## 2. `find`

```
FIND "共享"
FOUND 7
SCREEN s1 "网络文件和文件夹共享"
INPUT q1 "网络路径(N):" +set_text(text)
BUTTON b1 "共享(S)..."
SCREEN s2 "高级共享"
BUTTON b2 "高级共享(D)..."
SCREEN s4 "用户必须具有此计算机的用户帐户和密码，才能访问共享文件夹。…"
LINK lnk1 "网络和共享中心"
```

每条命中是完整实体行，包含 ref、属性与能力，可以直接用于后续调用。

## 3. `inspect`

```
INSPECT b2
@rev 1
BUTTON b2 "高级共享(D)..." +activate +focus +read
```

`inspect` 显示平时省略的隐含能力（如 `+focus`、`+read`）与关系（`acts_on=`、`label_for=`、`opens=`），并给出参数。

## 4. `call`

### 失败示例

```
CALL b5.activate()     # 点击禁用按钮
ERR TARGET_DISABLED b5 hint=inspect

CALL b2.submit()       # 该按钮没有 submit 能力
ERR CAPABILITY_NOT_FOUND b2 hint=inspect msg="no submit"

CALL b2.click()        # 原语动词被禁止
ERR PARSE_ERROR hint=fix-syntax msg="primitive verb 'click' is not allowed"
```

错误行携带 `hint=`，指向恢复动作。`activate` 以 `ACTION_NOT_SUPPORTED` 失败且节点仍暴露 `select` 时，错误行写 `hint=try select`；运行时不会自动改调 `select`。

### 成功示例

```
OBSERVE
@rev 1
WINDOW w1 "无标题 - Notepad"
  DOCUMENT tx1 "文本编辑器" selected focused +set_text(text) +scroll(…)
  PANE pn1 focused
    TAB t1
      LIST l1 selected
        TAB t2 "无标题. 未修改。" +close!
          TEXT tx2 "无标题"
          BUTTON b1 "关闭标签页"
      BUTTON b2 "添加新标签页"
  …
```

```
CALL tx1.set_text("Hello SomnusUI")
OK rev=2
  ~ tx1 value ->Hello SomnusUI
  + TAB t4 "Hello SomnusUI. 已修改。" +close!
  + TEXT tx9 "Hello SomnusUI"
  - t2
  - tx2
  - b1
```

增量是回复中信息密度最高的部分：`~` 表示值变化，`+` 表示新增节点，`-` 表示节点消失。模型无需重新 `observe` 就能知道发生了什么。

`OK` 表示动作执行且产生可见变化，或声明的 effects 全部满足。`OK_UNVERIFIED` 表示动作执行、没有观察到可见变化，也没有可验证的声明效果。`EFFECT_NOT_CONFIRMED` 表示声明了 effects 但没有满足。

## 5. 复现

```bash
# 通用模式，任意窗口
somnus --no-pack observe

# 指定窗口
somnus windows
somnus observe w1

# 交互式
somnus --no-pack repl

# 应用内置 pack；需先启动餐厅演示应用
somnus --pack restaurant-demo observe
```

在 Python 中：

```python
from somnus.providers.windows.uia import UiaProvider
from somnus.runtime.session import Session

session = Session(UiaProvider(), allow_packs=False)
print(session.observe().text)                  # 1. 概览
print(session.dispatch('FIND "共享"'))          # 2. 查找
print(session.dispatch("INSPECT b2"))           # 3. 详情
print(session.dispatch("CALL b2.activate()"))   # 4. 操作与增量
```
