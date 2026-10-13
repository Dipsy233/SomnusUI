# 一个窗口，LLM 能看到什么

以一个真实的 Windows 属性对话框为例，逐步展示 LLM 在四个工具下实际收到的内容。

---

## ① `observe` — 概览（304 tokens）

```
@rev 1
WINDOW w2 "任务栏"
WINDOW w3 "E:\0down\SomnusUI"
WINDOW w4 "Program Manager"
WINDOW w1 "agent 属性" +scroll_into_view
  SCREEN s1 "网络文件和文件夹共享"
  IMAGE im1 "网络文件和文件夹共享"
  TEXT tx1 "agent"
  TEXT tx2 "不共享"
  TEXT tx3 "网络路径(N):"
  INPUT q1 "网络路径(N):" readonly
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
  PANE pn1 +set_text(text)
    MENU mn1 "系统"
      MENUITEM mi1 "系统" +collapse +expand
    BUTTON b6 "关闭"
```

**LLM 从这里能直接读出的信息**：

| 信息 | 来源 |
|---|---|
| 有哪些窗口，哪个是当前目标 | `WINDOW` 行；目标在最前，带 `+scroll_into_view` |
| 界面的语义分区 | `SCREEN s1 "网络文件和文件夹共享"` 等 |
| 哪些是输入框 | `INPUT q1`（`readonly` 明确标注不可编辑） |
| 哪些能点 | `BUTTON b1 "共享(S)..."` |
| 哪个按钮当前不可用 | `BUTTON b5 "应用(A)" disabled` |
| 可用的操作 | 行内 `+activate`（省略时按角色隐含）、`+set_text(text)`、`+select` |
| 折叠的集合 | 无（这个窗口的 tab 全展开） |
| 长文本被截断 | `…` 表示超 80 字被裁剪 |

**LLM 永远看不到的**：控件句柄、坐标、`AutomationId`、进程 pid、COM 指针、
原始 UIA 的 `control_type` 字符串、8 层空 `Pane` 包装。

---

## ② `find` — 按关键词定位（7 条命中）

```
FIND "共享"
→ FOUND 7
SCREEN s1 "网络文件和文件夹共享"
INPUT q1 "网络路径(N):" readonly
BUTTON b1 "共享(S)..."
SCREEN s2 "高级共享"
BUTTON b2 "高级共享(D)..."
SCREEN s4 "用户必须具有此计算机的用户帐户和密码，才能访问共享文件夹。…"
LINK lnk1 "网络和共享中心"
```

返回的是**完整实体行**，不是只给 ref —— 省掉一次 `inspect`（开发文档 §6.3）。

---

## ③ `inspect` — 展开一个节点

```
INSPECT b2
→ @rev 1
  BUTTON b2 "高级共享(D)..." +activate +focus +read
```

`inspect` 会显示**隐含能力**（`+focus`、`+read` 平时省略）和**关系**
（`acts_on=` / `label_for=` / `opens=`，如果有）。

---

## ④ `call` — 操作，并返回 delta

### 操作被拒绝的三种情况

```
CALL b5.activate()     # 点一个 disabled 的按钮
→ ERR TARGET_DISABLED b5 hint=inspect

CALL b2.submit()       # 该按钮没有 submit 能力
→ ERR CAPABILITY_NOT_FOUND b2 hint=inspect msg="no submit"

CALL b2.click()        # 原语动词，禁止
→ ERR PARSE_ERROR hint=fix-syntax msg="primitive verb 'click' is not allowed"
```

每种错误都带 `hint=`，告诉 LLM 该怎么恢复。

### 成功的操作会产生 delta

以 Notepad 为例（同样是真实窗口）：

```
OBSERVE（有 pack）
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
  PANE pn4 disabled focused
    TEXT tx3 "行 1，\n列 1"
    TEXT tx4 "0 个字符"
    TEXT tx6 " Windows (CRLF)"
```

```
CALL tx1.set_text("Hello SomnusUI")
→ OK_UNVERIFIED rev=2
  ~ tx1 value →Hello SomnusUI          ← 值变了
  + TAB t4 "Hello SomnusUI. 已修改。" +close!   ← 标题跟着变
  + TEXT tx9 "Hello SomnusUI"
  - t2                                  ← 旧 tab 消失
  - tx2
  - b1
```

**delta 是最有价值的部分**：LLM 不用重新 `observe` 就知道发生了什么。
`~` 改了、`+` 新增、`-` 消失。

`OK_UNVERIFIED` 表示"动作执行了，但应用没声明可验证的效果"——
对比 `OK`（effects 全部满足）和 `EFFECT_NOT_CONFIRMED`（声明了但没满足）。

---

## 同一个窗口，UFO 会给什么

对照 `docs/comparison-ufo.md` 的实测：

| | SomnusUI | UFO |
|---|---|---|
| 上面那个属性窗口 | 304 tokens | 原始 JSON，含每个节点的 3 组矩形坐标 |
| QQ 窗口（实测） | 393 tokens / 42 行 | 58,175 字符 / ~14.5k tokens |
| 坐标 | 0 处 | 每节点 3 处 |
| 无名空容器 | flatten 掉 | 全部保留（曾见连续 8 层空 Pane） |
| 能力标注 | 行内 `+activate` / `+disabled` | 无，靠 `control_type` 猜 |
| 动作 | `CALL b2.activate()` | `click_input(id, name)` 或 `click_on_coordinates(x, y)` |
| 结果 | delta（`~` / `+` / `-`） | 返回值，需再 `get_ui_tree` 对比 |

---

## 复现

```bash
# 无 pack 的通用视图（任意窗口）
somnus --provider uia observe

# 查看某个窗口
somnus --provider uia windows
somnus --provider uia observe w1

# 交互式
somnus --provider uia --allow explorer.exe repl

# 带 pack
somnus --provider uia --allow notepad.exe --pack notepad observe
```

或在 Python 里：

```python
from somnus.providers.windows.uia import UiaProvider
from somnus.runtime.session import Session

session = Session(UiaProvider(allow_list=["notepad.exe"]), pack_id="notepad")
print(session.observe().text)                  # ① 概览
print(session.dispatch('FIND "编辑"'))          # ② 查找
print(session.dispatch("INSPECT mi2"))          # ③ 详情
print(session.dispatch("CALL mi2.expand()"))    # ④ 操作 + delta
```
