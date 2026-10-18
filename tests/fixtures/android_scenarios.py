"""Android scenario fixtures: the acceptance app's six screens as Portal JSON.

SomnusDroid doc §11.1. Each builder returns a Portal ``snapshot`` dict - the
same shape a real Portal would answer ``snapshot`` with - so the conversion,
compiler and runtime paths under test are exactly the phone path.

======================  =======================================================
``settings_tree``       RecyclerView settings list + search: virtualisation,
                        label promotion (clickable LinearLayout + TextViews).
``form_tree``           View-system form: writable/readonly fields, switch,
                        spinner, disabled button.
``chat_tree``           custom input (no SET_TEXT) + send: set_text routing.
``store_tree``          500-row catalog + search: FIND scroll, folded preview.
``web_tree``            WebView form: web nodes compile + fold.
``canvas_tree``         pure Canvas surface: must compile to `? reason=`.
======================  =======================================================
"""

from __future__ import annotations

from typing import Any

CLICK = "CLICK"
LONG_CLICK = "LONG_CLICK"
SET_TEXT = "SET_TEXT"
SCROLL_FORWARD = "SCROLL_FORWARD"
SCROLL_BACKWARD = "SCROLL_BACKWARD"
SHOW_ON_SCREEN = "SHOW_ON_SCREEN"


def _node(
    key: str,
    cls: str,
    *,
    text: str | None = None,
    desc: str | None = None,
    rid: str | None = None,
    actions: list[str] | None = None,
    enabled: bool = True,
    editable: bool = False,
    password: bool = False,
    checked: bool | None = None,
    selected: bool = False,
    bounds: tuple[int, int, int, int] | None = (0, 0, 1080, 2400),
    collection: dict[str, Any] | None = None,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "cls": cls,
        "text": text,
        "desc": desc,
        "rid": rid,
        "actions": actions or [],
        "enabled": enabled,
        "editable": editable,
        "password": password,
        "checked": checked,
        "selected": selected,
        "bounds": list(bounds) if bounds else None,
        "collection": collection,
        "children": children or [],
    }


def _window(
    wid: int,
    pkg: str,
    title: str,
    root: dict[str, Any],
    *,
    display: int = 0,
    active: bool = True,
) -> dict[str, Any]:
    return {
        "display": display,
        "rev": 1,
        "windows": [
            {
                "id": wid,
                "type": "application",
                "pkg": pkg,
                "title": title,
                "active": active,
                "pid": 4242,
                "root": root,
            }
        ],
    }


# ---------------------------------------------------------------------------
# settings: label promotion + virtualised list
# ---------------------------------------------------------------------------


def settings_row(i: int, title: str, sub: str) -> dict[str, Any]:
    return _node(
        f"12/1.0.{i}", "android.widget.LinearLayout",
        actions=[CLICK],
        rid=f"com.android.settings:id/preference_{i}",
        children=[
            _node(f"12/1.0.{i}.0", "android.widget.TextView", text=title,
                  rid="android:id/title"),
            _node(f"12/1.0.{i}.1", "android.widget.TextView", text=sub,
                  rid="android:id/summary"),
        ],
    )


def settings_tree() -> dict[str, Any]:
    rows = [
        settings_row(0, "WLAN", "已连接到 Home-5G"),
        settings_row(1, "蓝牙", "已关闭"),
        settings_row(2, "显示", ""),
        settings_row(3, "声音", ""),
        settings_row(4, "应用", ""),
        settings_row(5, "通知", ""),
        settings_row(6, "电池", "87%"),
        settings_row(7, "存储", "已用 64 GB"),
    ]
    search = _node(
        "12/0.0", "android.widget.EditText",
        text="搜索设置", actions=[CLICK, SET_TEXT], editable=True,
        rid="com.android.settings:id/search",
    )
    list_node = _node(
        "12/1.0", "androidx.recyclerview.widget.RecyclerView",
        actions=[SCROLL_FORWARD, SCROLL_BACKWARD],
        rid="com.android.settings:id/recycler_view",
        collection={"count": -1, "virtualized": True},
        children=rows,
    )
    root = _node(
        "12/0", "android.widget.FrameLayout",
        desc="设置", rid="com.android.settings:id/main_content",
        children=[search, list_node],
    )
    return _window(12, "com.android.settings", "设置", root)


# ---------------------------------------------------------------------------
# form
# ---------------------------------------------------------------------------


def form_tree(
    *,
    name: str = "",
    agreed: bool = False,
    validation: str = "未提交",
) -> dict[str, Any]:
    root = _node("20/0", "android.widget.ScrollView", desc="表单",
                 actions=[SCROLL_FORWARD],
                 children=[
                     _node("20/0.0", "android.widget.EditText", text=name,
                           desc="姓名", actions=[CLICK, SET_TEXT], editable=True,
                           rid="form:name"),
                     _node("20/0.1", "android.widget.TextView", text="只读编号 REQ-0001",
                           rid="form:id"),
                     _node("20/0.2", "android.widget.Switch", text="同意条款",
                           actions=[CLICK], checked=agreed,
                           rid="form:agree"),
                     _node("20/0.3", "android.widget.Button", text="提交",
                           actions=[CLICK], rid="form:submit"),
                     _node("20/0.4", "android.widget.Button", text="禁用按钮",
                           actions=[CLICK], enabled=False, rid="form:disabled"),
                     _node("20/0.5", "android.widget.TextView", text=validation,
                           rid="form:validation"),
                 ])
    return _window(20, "com.example.form", "表单", root)


# ---------------------------------------------------------------------------
# chat: composer without SET_TEXT (the set_text routing case)
# ---------------------------------------------------------------------------


def chat_tree(
    *,
    composer_text: str = "",
    sent: list[str] | None = None,
) -> dict[str, Any]:
    sent = sent or []
    messages = [
        _node(f"30/1.0.{i}", "android.widget.TextView", text=line,
              rid=f"chat:msg_{i}")
        for i, line in enumerate(["历史消息 1", "历史消息 2"] + sent)
    ]
    root = _node("30/0", "android.widget.LinearLayout", desc="群组 1",
                 children=[
                     _node("30/0.0", "androidx.recyclerview.widget.RecyclerView",
                           actions=[SCROLL_FORWARD],
                           collection={"count": -1, "virtualized": True},
                           children=messages),
                     # A self-drawn composer: no SET_TEXT, not editable, but
                     # clickable - the hard routing case (doc §7.2). It reads
                     # through its text, but publishes no write action.
                     _node("30/0.1", "com.example.chat.ProseMirrorEditor",
                           text=composer_text, desc="消息编辑器",
                           actions=[CLICK], rid="chat:composer"),
                     _node("30/0.2", "android.widget.Button", text="发送",
                           actions=[CLICK], rid="chat:send"),
                 ])
    return _window(30, "com.example.chat", "聊天", root)


# ---------------------------------------------------------------------------
# store: 500-row catalog, folded preview + FIND scroll
# ---------------------------------------------------------------------------


def store_tree(*, rows: int = 500, query: str = "") -> dict[str, Any]:
    def item(i: int) -> dict[str, Any]:
        name = f"Special Item {i}" if i % 50 == 17 else f"Item {i}"
        return _node(
            f"40/1.0.{i}", "android.widget.LinearLayout", actions=[CLICK],
            children=[
                _node(f"40/1.0.{i}.0", "android.widget.TextView", text=name),
                _node(f"40/1.0.{i}.1", "android.widget.TextView", text=f"¥{(i % 90) + 9}.99"),
            ],
        )

    # Only the first `window` rows are realised (a RecyclerView's on-screen
    # rows); the rest exist but are virtualised away - that is exactly the
    # state a real list reports, and what FIND scroll walks.
    window = min(rows, 20)
    root = _node("40/0", "android.widget.FrameLayout", desc="商店",
                 children=[
                     _node("40/0.0", "android.widget.EditText", text=query,
                           desc="搜索", actions=[CLICK, SET_TEXT], editable=True,
                           rid="store:search"),
                     _node("40/1.0", "androidx.recyclerview.widget.RecyclerView",
                           actions=[SCROLL_FORWARD, SCROLL_BACKWARD],
                           collection={"count": rows, "virtualized": True},
                           children=[item(i) for i in range(window)]),
                 ])
    return _window(40, "com.example.store", "商店", root)


# ---------------------------------------------------------------------------
# web: WebView form
# ---------------------------------------------------------------------------


def web_tree() -> dict[str, Any]:
    root = _node("50/0", "android.webkit.WebView", desc="登录",
                 children=[
                     _node("50/0.0", "android.view.View", desc="登录表单",
                           children=[
                               _node("50/0.0.0", "android.widget.EditText",
                                     text="", desc="用户名",
                                     actions=[CLICK, SET_TEXT], editable=True),
                               _node("50/0.0.1", "android.widget.EditText",
                                     text="", desc="密码", password=True,
                                     actions=[CLICK, SET_TEXT], editable=True),
                               _node("50/0.0.2", "android.view.View",
                                     text="登录", actions=[CLICK]),
                           ]),
                 ])
    return _window(50, "com.example.browser", "浏览器", root)


# ---------------------------------------------------------------------------
# canvas: opaque surface - must compile to `? ref reason=`
# ---------------------------------------------------------------------------


def canvas_tree() -> dict[str, Any]:
    root = _node("60/0", "android.widget.FrameLayout", desc="游戏",
                 children=[
                     # The whole surface is self-drawn: no actions, no text,
                     # no class identity - the Portal honestly reports this.
                     _node("60/0.0", "android.view.View", bounds=None),
                 ])
    return _window(60, "com.example.game", "游戏", root)


SCREENS: dict[str, Callable[[], dict[str, Any]]] = {
    "settings": settings_tree,
    "form": form_tree,
    "chat": chat_tree,
    "store": store_tree,
    "web": web_tree,
    "canvas": canvas_tree,
}
