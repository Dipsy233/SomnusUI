"""Fixture builders for the Scenario Suite acceptance app.

`apps/scenario-suite` is the self-contained WPF app the test suite drives. It
replaces the older reliance on Notepad and on the tiny three-product restaurant
screen: each scenario covers a class of real-world difficulty that the runtime
has to survive, and each is reproducible without a desktop (these builders) and
against the real window (``-m windows``).

The scenarios, and what each one is *for*:

======================  =========================================================
``store_tree``          search box + 240-row scrollable catalog + cart. Large
                        data, FIND into a folded collection, action records.
``form_tree``           labelled fields (one writable, one read-only), checkbox,
                        combo, disabled button, gated submit, submit record.
``chat_tree``           rich-text composer with **no native write interface**,
                        message history, send button and a visible send log. This
                        is the screen that exercises the ``set_text`` fallback
                        chain rather than assuming it.
``console_tree``        a *plain* text box composer (Value pattern), so the
                        native write route can be shown to win over the
                        synthetic channels on equal footing.
``tree_tree``           a nested expandable tree: the same screen is ~6 lines
                        collapsed or thousands expanded, which is the
                        compression-by-expansion-level case.
======================  =========================================================

Every builder mirrors the accessibility shape of the corresponding XAML, not its
C# implementation: nodes carry the same names, roles, patterns and states UIA
reports for that control, because the routing decisions under test depend on
those and on nothing else.
"""

from __future__ import annotations

from typing import Callable

from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

# ---------------------------------------------------------------------------
# shared node helpers
# ---------------------------------------------------------------------------

#: Pattern sets per control kind, mirroring what UIA actually reports. These are
#: the evidence the resolver reads, so they are spelled out per control rather
#: than guessed from the role.
P_INVOKE = frozenset({"invoke"})
P_TEXT = frozenset({"text"})
P_VALUE = frozenset({"value", "text"})
P_LIST = frozenset({"scroll", "selection"})
P_SCROLL = frozenset({"scroll"})
P_SELECT = frozenset({"selectionitem", "invoke"})
P_EXPAND = frozenset({"expandcollapse"})
P_EXPAND_ITEM = frozenset({"expandcollapse", "selectionitem"})
#: A **self-drawn** rich-text composer, as UIA reports one: a ``group`` with
#: the toolkit's own class name, a Text interface for reading, no Value pattern
#: and no element HWND. The Scenario Suite's ``SelfDrawnEditor`` reproduces the
#: shape in WPF, where the peer adds ``legacyiaccessible`` (the MSAA bridge) and
#: the Text pattern is what its ``ITextProvider`` publishes - so the readable
#: surface is ``text``, and there is nothing to *write* through.
#:
#: This is the hard case, and the one this screen exists for: the control
#: publishes nothing the runtime can write through, so ``set_text`` must fall
#: to the two synthetic channels - both off by default, both reported when
#: refused.
P_RICHTEXT = frozenset({"legacyiaccessible", "text"})


class _Ids:
    """Per-window native id allocator, so every node is individually reachable."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return 10_000 + self._n


def _node(
    wid: str,
    role: str,
    name: str | None = None,
    *,
    ids: _Ids,
    value: str | None = None,
    patterns: frozenset[str] = frozenset(),
    states: frozenset[str] = frozenset(),
    automation_id: str | None = None,
    cls: str | None = None,
    hwnd: int = 0,
    children: list[RawNode] | None = None,
) -> RawNode:
    rid = ids.next()
    return RawNode(
        native=NativeRef(
            window_id=wid,
            runtime_id=(rid,),
            automation_id=automation_id,
            class_name=cls,
            native_window_handle=hwnd,
            path=(rid,),
        ),
        role=role,
        name=name,
        value=value,
        patterns=patterns,
        states=states,
        children=children or [],
    )


def _window(wid: str, title: str, process: str, root: RawNode) -> RawTree:
    return RawTree(
        window=WindowInfo(
            window_id=wid,
            title=title,
            process=process,
            pid=6000,
            active=True,
        ),
        root=root,
        raw_revision=1,
    )


# ---------------------------------------------------------------------------
# store: search + large catalog + cart
# ---------------------------------------------------------------------------

#: Item count of the default catalog. Deliberately far past every observation
#: budget, so a test asserting "the observation stays small" is meaningful.
STORE_ITEMS = 240


def store_catalog(count: int = STORE_ITEMS) -> list[tuple[str, float, int]]:
    """(name, price, stock) rows, chosen so FIND has a unique and a common hit."""
    rows: list[tuple[str, float, int]] = []
    for i in range(count):
        name = f"Special Item {i}" if i % 17 == 0 else f"Item {i}"
        rows.append((name, 1.0 + (i % 40) * 0.5, 1 + (i % 25)))
    return rows


def store_tree(
    *,
    catalog: list[tuple[str, float, int]] | None = None,
    cart: list[str] | None = None,
    cart_count: int = 0,
    cart_total: float = 0.0,
    query: str = "",
    window_id: str = "w1",
) -> RawTree:
    """The store screen: search box, product list, cart and last-action line."""
    rows = catalog if catalog is not None else store_catalog()
    cart = cart or []
    ids = _Ids()

    def product(idx: int, name: str, price: float, stock: int) -> RawNode:
        aid = f"store_item_{idx}"
        return _node(
            window_id,
            "group",
            ids=ids,
            automation_id=aid,
            children=[
                _node(window_id, "text", name, ids=ids, patterns=P_TEXT,
                      automation_id=f"{aid}_title"),
                _node(window_id, "text", f"${price:.2f}", ids=ids, patterns=P_TEXT,
                      automation_id=f"{aid}_price"),
                _node(window_id, "text", f"库存 {stock}", ids=ids, patterns=P_TEXT,
                      automation_id=f"{aid}_stock"),
                _node(window_id, "button", "加入", ids=ids, patterns=P_INVOKE,
                      automation_id=f"{aid}_add"),
            ],
        )

    products = _node(
        window_id,
        "list",
        "商品",
        ids=ids,
        patterns=P_LIST,
        automation_id="store_products",
        children=[
            product(i, name, price, stock)
            for i, (name, price, stock) in enumerate(rows)
        ],
    )

    search = _node(
        window_id,
        "edit",
        "搜索",
        ids=ids,
        value=query,
        patterns=P_VALUE,
        automation_id="store_search",
    )
    search_button = _node(
        window_id, "button", "搜索", ids=ids, patterns=P_INVOKE,
        automation_id="store_search_button",
    )
    result_summary = _node(
        window_id, "text", f"{len(rows)} 件商品", ids=ids, patterns=P_TEXT,
        automation_id="store_result_summary",
    )

    cart_items = _node(
        window_id,
        "list",
        "购物车明细",
        ids=ids,
        patterns=P_LIST,
        automation_id="store_cart_items",
        children=[
            _node(window_id, "listitem", line, ids=ids, patterns=P_SELECT)
            for line in cart
        ],
    )
    cart_group = _node(
        window_id,
        "group",
        "购物车",
        ids=ids,
        automation_id="store_cart",
        children=[
            _node(window_id, "text", f"{cart_count} items ${cart_total:.2f}",
                  ids=ids, patterns=P_TEXT, automation_id="store_cart_count"),
            cart_items,
            _node(window_id, "button", "结算", ids=ids, patterns=P_INVOKE,
                  automation_id="store_checkout"),
            _node(window_id, "text", "就绪", ids=ids, patterns=P_TEXT,
                  automation_id="store_last_action"),
        ],
    )

    root = _node(
        window_id,
        "pane",
        "store",
        ids=ids,
        automation_id="store_window",
        children=[search, search_button, result_summary, products, cart_group],
    )
    return _window(window_id, "Scenario Suite - store", "scenariosuite.exe", root)


# ---------------------------------------------------------------------------
# form: fields, validation, submit record
# ---------------------------------------------------------------------------


def form_tree(
    *,
    name: str = "",
    email: str = "",
    notes: str = "",
    record_id: str = "REQ-0001",
    validation: str = "未提交",
    records: list[str] | None = None,
    agreed: bool = False,
    window_id: str = "w1",
) -> RawTree:
    """The form screen: writable fields, a read-only field and a submit log."""
    ids = _Ids()
    records = records or ["（尚无提交）"]

    def labelled(label: str, role: str, aid: str, **kw) -> list[RawNode]:
        return [
            _node(window_id, "text", label, ids=ids, patterns=P_TEXT,
                  automation_id=f"{aid}_label"),
            _node(window_id, role, label, ids=ids, automation_id=aid, **kw),
        ]

    fields: list[RawNode] = []
    fields += labelled("姓名", "edit", "form_name", value=name, patterns=P_VALUE)
    fields += labelled("邮箱", "edit", "form_email", value=email, patterns=P_VALUE)
    fields += labelled("备注", "edit", "form_notes", value=notes,
                       patterns=P_VALUE | P_SCROLL)
    fields += labelled("只读编号", "edit", "form_id", value=record_id,
                       patterns=P_VALUE, states=frozenset({"readonly"}))

    agree = _node(window_id, "checkbox", "同意条款", ids=ids,
                  patterns=frozenset({"toggle"}),
                  states=frozenset({"checked"}) if agreed else frozenset(),
                  automation_id="form_agree")
    priority = _node(window_id, "select", "优先级", ids=ids, patterns=P_EXPAND,
                     automation_id="form_priority")

    controls = [
        agree,
        priority,
        _node(window_id, "button", "提交", ids=ids, patterns=P_INVOKE,
              automation_id="form_submit"),
        _node(window_id, "button", "重置", ids=ids, patterns=P_INVOKE,
              automation_id="form_reset"),
        _node(window_id, "button", "禁用按钮", ids=ids, patterns=P_INVOKE,
              states=frozenset({"disabled"}), automation_id="form_disabled"),
        _node(window_id, "text", validation, ids=ids, patterns=P_TEXT,
              automation_id="form_validation"),
    ]

    records_list = _node(
        window_id,
        "list",
        "提交记录",
        ids=ids,
        patterns=P_LIST,
        automation_id="form_records_list",
        children=[
            _node(window_id, "listitem", line, ids=ids, patterns=P_SELECT)
            for line in records
        ],
    )
    records_group = _node(
        window_id, "group", "提交记录", ids=ids, automation_id="form_records",
        children=[records_list],
    )

    root = _node(
        window_id,
        "pane",
        "form",
        ids=ids,
        automation_id="form_window",
        children=[*fields, *controls, records_group],
    )
    return _window(window_id, "Scenario Suite - form", "scenariosuite.exe", root)


# ---------------------------------------------------------------------------
# chat: rich-text composer, history, send log
# ---------------------------------------------------------------------------

CHAT_CONVERSATIONS = 12
CHAT_HISTORY = 8


def chat_tree(
    *,
    conversations: int = CHAT_CONVERSATIONS,
    history: int = CHAT_HISTORY,
    selected: int = 0,
    composer_text: str = "",
    sent: list[str] | None = None,
    outbox: list[str] | None = None,
    window_id: str = "w1",
) -> RawTree:
    """The chat screen with a rich-text composer (no native write interface).

    ``composer_text`` is the composer's *document text*: UIA can read it through
    the Text pattern but cannot set it, which is why the runtime must fall back
    to the clipboard or the keyboard route. ``sent``/``outbox`` are the message
    history and the send log - the verification surface.
    """
    ids = _Ids()
    sent = sent or []
    outbox = outbox or []

    conv_rows = [
        _node(
            window_id,
            "listitem",
            f"群组 {i + 1}",
            ids=ids,
            patterns=P_SELECT,
            states=frozenset({"selected"}) if i == selected else frozenset(),
            automation_id=f"chat_conv_{i}",
        )
        for i in range(conversations)
    ]
    conversations_box = _node(
        window_id, "list", "会话", ids=ids, patterns=P_LIST,
        automation_id="chat_conversations", children=conv_rows,
    )

    messages = [
        _node(window_id, "text", f"历史消息 {m + 1} @群组{selected + 1}", ids=ids,
              patterns=P_TEXT, automation_id=f"chat_history_{selected}_{m}")
        for m in range(history)
    ]
    messages += [
        _node(window_id, "text", line, ids=ids, patterns=P_TEXT,
              automation_id=f"chat_sent_{i}")
        for i, line in enumerate(sent)
    ]
    message_list = _node(
        window_id, "list", "消息", ids=ids, patterns=P_LIST,
        automation_id="chat_messages", children=messages,
    )

    composer = _node(
        window_id,
        # `group`, not `document`: a self-drawn editor is a painted surface the
        # platform knows nothing about, not a document control.
        "group",
        "消息编辑器",
        ids=ids,
        value=composer_text,
        patterns=P_RICHTEXT,
        automation_id="chat_composer",
        cls="ProseMirror ExEditor-qq-msg-editor",
    )
    send = _node(window_id, "button", "发送", ids=ids, patterns=P_INVOKE,
                 automation_id="chat_send")

    outbox_list = _node(
        window_id,
        "list",
        "发送记录",
        ids=ids,
        patterns=P_LIST,
        automation_id="chat_outbox_list",
        children=[
            _node(window_id, "listitem", line, ids=ids, patterns=P_SELECT)
            for line in outbox
        ],
    )
    outbox_group = _node(
        window_id, "group", "发送记录", ids=ids, automation_id="chat_outbox",
        children=[outbox_list],
    )

    root = _node(
        window_id,
        "pane",
        "chat",
        ids=ids,
        automation_id="chat_window",
        children=[
            conversations_box,
            _node(window_id, "text", f"群组 {selected + 1}", ids=ids,
                  patterns=P_TEXT, automation_id="chat_title"),
            message_list,
            composer,
            send,
            outbox_group,
        ],
    )
    return _window(window_id, "Scenario Suite - chat", "scenariosuite.exe", root)


# ---------------------------------------------------------------------------
# console: plain text box composer (contrast case)
# ---------------------------------------------------------------------------


def console_tree(
    *,
    contacts: int = 10,
    selected: int = 0,
    thread: list[str] | None = None,
    draft: str = "",
    status: str = "就绪",
    window_id: str = "w1",
) -> RawTree:
    """The console screen: contacts, a thread, a plain Value-pattern composer.

    The composer here *does* publish ``value``, so the native write route must
    win; the rich-text twin in :func:`chat_tree` is what forces the fallback.
    """
    ids = _Ids()
    thread = thread or [f"（与 联系人 {selected + 1} 的历史消息）"]

    contact_rows = [
        _node(
            window_id,
            "group",
            ids=ids,
            patterns=P_SELECT,
            states=frozenset({"selected"}) if i == selected else frozenset(),
            automation_id=f"console_contact_{i}",
            children=[
                _node(window_id, "text", f"联系人 {i + 1}", ids=ids,
                      patterns=P_TEXT, automation_id=f"console_contact_{i}_name"),
                _node(window_id, "button", "打开", ids=ids, patterns=P_INVOKE,
                      automation_id=f"console_contact_{i}_open"),
            ],
        )
        for i in range(contacts)
    ]
    contacts_list = _node(
        window_id, "list", "联系人", ids=ids, patterns=P_LIST,
        automation_id="console_contacts", children=contact_rows,
    )

    thread_list = _node(
        window_id,
        "list",
        "消息",
        ids=ids,
        patterns=P_LIST,
        automation_id="console_thread",
        children=[
            _node(window_id, "text", line, ids=ids, patterns=P_TEXT,
                  automation_id=f"console_thread_{i}")
            for i, line in enumerate(thread)
        ],
    )

    input_box = _node(
        window_id, "edit", "输入", ids=ids, value=draft, patterns=P_VALUE,
        automation_id="console_input",
    )

    root = _node(
        window_id,
        "pane",
        "console",
        ids=ids,
        automation_id="console_window",
        children=[
            contacts_list,
            _node(window_id, "text", f"联系人 {selected + 1}", ids=ids,
                  patterns=P_TEXT, automation_id="console_title"),
            thread_list,
            input_box,
            _node(window_id, "button", "发送", ids=ids, patterns=P_INVOKE,
                  automation_id="console_send"),
            _node(window_id, "text", status, ids=ids, patterns=P_TEXT,
                  automation_id="console_status"),
        ],
    )
    return _window(window_id, "Scenario Suite - console", "scenariosuite.exe", root)


# ---------------------------------------------------------------------------
# tree: nested, expandable (compression by expansion level)
# ---------------------------------------------------------------------------

#: Fan-out per level. 6*6*6*6 = 1296 leaves under one root, which is the point:
#: the collapsed form is a handful of lines and the expanded form is thousands.
TREE_FANOUT = 6
TREE_DEPTH = 4


def _tree_node(
    wid: str,
    ids: _Ids,
    prefix: str,
    label: str,
    depth: int,
    *,
    expanded: set[str],
    fanout: int,
    max_depth: int,
) -> RawNode:
    aid = prefix
    children: list[RawNode] = []
    if depth < max_depth:
        children = [
            _tree_node(wid, ids, f"{prefix}_{i}", f"{label}.{i}", depth + 1,
                       expanded=expanded, fanout=fanout, max_depth=max_depth)
            for i in range(fanout)
        ]
    is_expanded = aid in expanded
    states: set[str] = set()
    if children:
        states.add("expanded" if is_expanded else "collapsed")
        if not is_expanded:
            # A collapsed node reports its children as virtualized: the provider
            # knows they exist but has not realized them. That is what makes the
            # expansion level the token dial.
            return _node(
                wid, "treeitem", label, ids=ids, patterns=P_EXPAND_ITEM,
                states=frozenset(states), automation_id=aid,
            )
    return _node(
        wid, "treeitem", label, ids=ids, patterns=P_EXPAND_ITEM,
        states=frozenset(states), automation_id=aid, children=children,
    )


def tree_tree(
    *,
    expanded: set[str] | None = None,
    fanout: int = TREE_FANOUT,
    max_depth: int = TREE_DEPTH,
    window_id: str = "w1",
) -> RawTree:
    """A nested tree; ``expanded`` selects which branches are realized.

    With an empty ``expanded`` set the whole tree is one collapsed root. Each
    id added to the set realizes that branch's children, so the observation
    grows with the expansion level and stays proportional to what was asked for.
    """
    ids = _Ids()
    expanded = expanded or set()
    root_node = _tree_node(
        window_id, ids, "tree_root", "根目录", 0,
        expanded=expanded, fanout=fanout, max_depth=max_depth,
    )
    tree_view = _node(
        window_id, "tree", "目录树", ids=ids, patterns=P_SCROLL,
        automation_id="tree_root", children=[root_node],
    )
    status = _node(
        window_id, "text", f"节点总数 {_count_nodes(root_node)}", ids=ids,
        patterns=P_TEXT, automation_id="tree_status",
    )
    root = _node(
        window_id,
        "pane",
        "tree",
        ids=ids,
        automation_id="tree_window",
        children=[
            _node(window_id, "button", "全部展开", ids=ids, patterns=P_INVOKE,
                  automation_id="tree_expand_all"),
            _node(window_id, "button", "全部折叠", ids=ids, patterns=P_INVOKE,
                  automation_id="tree_collapse_all"),
            status,
            tree_view,
        ],
    )
    return _window(window_id, "Scenario Suite - tree", "scenariosuite.exe", root)


def _count_nodes(node: RawNode) -> int:
    return sum(1 for _ in node.walk())


# ---------------------------------------------------------------------------
# native: real Win32 controls (an HWND, a Value pattern)
# ---------------------------------------------------------------------------


def native_tree(
    *,
    single: str = "",
    multiline: str = "",
    readonly: str = "LOCKED",
    rich: str = "",
    status: str | None = None,
    window_id: str = "w1",
) -> RawTree:
    """The native screen: Win32 EDIT controls hosted in WPF.

    Every control here maps a real HWND (the ``hwnd`` field is non-zero), which
    is what makes them the native-control case: the shape the accessibility tree
    reports for an application that uses real Win32 controls rather than WPF's
    painted ones. They also publish ``value``, so the ordinary Value route
    writes them - the point of the screen is that reachable, real native shapes
    are covered, not that a special route runs.

    ``readonly`` maps a handle but no ``set_text`` at all, which is the honest
    answer for a control the platform refuses to write.
    """
    ids = _Ids()
    if status is None:
        status = f'单行="{single}" 多行="{multiline}" 富文本="{rich}"'

    root = _node(
        window_id,
        "pane",
        "native",
        ids=ids,
        automation_id="native_window",
        children=[
            _node(window_id, "text", "单行(原生 EDIT):", ids=ids,
                  patterns=P_TEXT, automation_id="native_edit_label"),
            _node(window_id, "edit", "单行输入", ids=ids, value=single,
                  patterns=P_VALUE, automation_id="native_edit", hwnd=0xA001),
            _node(window_id, "text", "多行(原生):", ids=ids,
                  patterns=P_TEXT, automation_id="native_multiline_label"),
            _node(window_id, "edit", "多行输入", ids=ids, value=multiline,
                  patterns=P_VALUE, automation_id="native_multiline", hwnd=0xA002),
            _node(window_id, "text", "只读(原生):", ids=ids,
                  patterns=P_TEXT, automation_id="native_readonly_label"),
            _node(window_id, "edit", "只读输入", ids=ids, value=readonly,
                  patterns=P_TEXT, states=frozenset({"readonly"}),
                  automation_id="native_readonly", hwnd=0xA003),
            _node(window_id, "text", "RichEdit(原生):", ids=ids,
                  patterns=P_TEXT, automation_id="native_rich_label"),
            _node(window_id, "edit", "富文本输入", ids=ids, value=rich,
                  patterns=P_VALUE, automation_id="native_rich", hwnd=0xA004),
            _node(window_id, "button", "复制到状态", ids=ids, patterns=P_INVOKE,
                  automation_id="native_copy"),
            _node(window_id, "button", "清空", ids=ids, patterns=P_INVOKE,
                  automation_id="native_clear"),
            _node(window_id, "text", status, ids=ids, patterns=P_TEXT,
                  automation_id="native_status"),
        ],
    )
    return _window(window_id, "Scenario Suite - native", "scenariosuite.exe", root)


# ---------------------------------------------------------------------------
# menu: a menu bar and a context menu
# ---------------------------------------------------------------------------


def _menubar_entry(
    window_id: str,
    ids: _Ids,
    label: str,
    items: list[tuple[str, str]],
    open_menu: str | None,
) -> RawNode:
    """One menu bar entry, with its items realized only when it is open."""
    children = []
    if open_menu == label:
        children = [
            _node(window_id, "menuitem", item_label, ids=ids,
                  patterns=P_INVOKE, automation_id=f"menu_item_{aid}")
            for aid, item_label in items
        ]
    return _node(
        window_id, "menu", label, ids=ids, patterns=P_INVOKE,
        states=frozenset({"expanded"} if open_menu == label else {"collapsed"}),
        automation_id=f"menu_bar_{label}", children=children,
    )


def menu_tree(
    *,
    open_menu: str | None = None,
    last_action: str = "就绪",
    context_open: bool = False,
    window_id: str = "w1",
) -> RawTree:
    """A window with a menu bar, driven the way a real one is.

    ``open_menu`` names the menu bar entry whose submenu is showing; its items
    are only realized then. That is the property that matters: a menu is a
    *navigation* - the items do not exist in the tree until the menu is opened,
    so an agent must activate the bar entry first and read the delta, exactly as
    with a real Windows menu.

    ``context_open`` shows the target row's context menu instead - the same
    shape the runtime's ``context_menu`` gesture produces.
    """
    ids = _Ids()

    # Allocate the container's id before the entries, so the menubar owns the
    # lowest id in its subtree (a stable, readable order rather than one that
    # depends on how the children happen to be built).
    menubar = _node(
        window_id, "menubar", "菜单栏", ids=ids,
        children=[
            _menubar_entry(window_id, ids, label, items, open_menu)
            for label, items in _MENU_ITEMS.items()
        ],
    )

    # The document area: one row that owns a context menu.
    row = _node(
        window_id, "listitem", "文档 1", ids=ids, patterns=P_SELECT,
        automation_id="menu_row",
    )
    document = _node(
        window_id, "list", "文档", ids=ids, patterns=P_LIST,
        automation_id="menu_documents", children=[row],
    )

    root_children = [
        menubar,
        document,
        _node(window_id, "text", last_action, ids=ids, patterns=P_TEXT,
              automation_id="menu_status"),
    ]
    if context_open:
        # The context menu attached to the row, as the runtime mounts it: it is
        # part of the same tree, under the node that owns it.
        root_children.append(
            _node(
                window_id, "menu", "上下文", ids=ids,
                children=[
                    _node(window_id, "menuitem", "重命名", ids=ids, patterns=P_INVOKE,
                          automation_id="menu_ctx_rename"),
                    _node(window_id, "menuitem", "删除", ids=ids, patterns=P_INVOKE,
                          automation_id="menu_ctx_delete"),
                ],
            )
        )

    root = _node(
        window_id, "pane", "menu", ids=ids, automation_id="menu_window",
        children=root_children,
    )
    return _window(window_id, "Scenario Suite - menu", "scenariosuite.exe", root)


# ---------------------------------------------------------------------------
# dialog: a modal confirmation that appears on demand
# ---------------------------------------------------------------------------


def dialog_tree(
    *,
    dialog_open: bool = False,
    confirmed: bool = False,
    rows: list[str] | None = None,
    window_id: str = "w1",
) -> RawTree:
    """A list with a destructive action that opens a confirmation dialog.

    ``dialog_open`` mounts the dialog as a ``role=dialog`` node under the
    window root - the shape the compiler treats as a dialog layer and prefixes
    ``d``. Its confirm/cancel buttons are only in the tree while it is open,
    which is the property a real modal has (and the reason an agent must read
    the delta to find them rather than guessing their refs).

    ``confirmed`` reflects that the deletion went through: the last row is gone
    and the status says so.
    """
    ids = _Ids()
    rows = rows if rows is not None else ["报告 A", "报告 B", "报告 C"]

    row_nodes = [
        _node(window_id, "listitem", label, ids=ids, patterns=P_SELECT,
              automation_id=f"dialog_row_{i}")
        for i, label in enumerate(rows)
    ]
    document = _node(
        window_id, "list", "报告", ids=ids, patterns=P_LIST,
        automation_id="dialog_rows", children=row_nodes,
    )

    status = "已删除" if confirmed else "就绪"
    root_children = [
        document,
        _node(window_id, "button", "删除", ids=ids, patterns=P_INVOKE,
              automation_id="dialog_delete"),
        _node(window_id, "text", status, ids=ids, patterns=P_TEXT,
              automation_id="dialog_status"),
    ]
    if dialog_open:
        root_children.append(
            _node(
                window_id, "dialog", "确认删除", ids=ids,
                states=frozenset({"modal"}),
                children=[
                    _node(window_id, "text", "确定要删除最后一项吗？", ids=ids,
                          patterns=P_TEXT, automation_id="dialog_message"),
                    _node(window_id, "button", "确定", ids=ids, patterns=P_INVOKE,
                          automation_id="dialog_confirm"),
                    _node(window_id, "button", "取消", ids=ids, patterns=P_INVOKE,
                          automation_id="dialog_cancel"),
                ],
            )
        )

    root = _node(
        window_id, "pane", "dialog", ids=ids, automation_id="dialog_window",
        children=root_children,
    )
    return _window(window_id, "Scenario Suite - dialog", "scenariosuite.exe", root)


# ---------------------------------------------------------------------------
# scripted mutations for the mock provider
# ---------------------------------------------------------------------------


def _find(root: RawNode, automation_id: str) -> RawNode | None:
    for node in root.walk():
        if node.native.automation_id == automation_id:
            return node
    return None


def _row_of(root: RawNode, node: RawNode) -> RawNode:
    """The row (parent group) that contains ``node``, or ``node`` itself.

    A per-row action targets a button *inside* the row, so the row's title and
    price live on its siblings. The raw tree has no parent links, so the parent
    is found by searching for the one node whose children contain ``node``.
    """
    for candidate in root.walk():
        if any(child is node for child in candidate.children):
            return candidate
    return node


def _row_title(node: RawNode) -> str:
    """The title text of a product/contact row (its first named text child)."""
    for child in node.children:
        if child.role == "text" and child.name:
            return child.name
    return node.name or ""


def _row_price(node: RawNode) -> float:
    """The price text of a product row, as a number."""
    import re

    for child in node.children:
        if child.role == "text" and child.name:
            m = re.search(r"\$?(\d+\.\d{2})", child.name)
            if m:
                return float(m.group(1))
    return 0.0


def _make_item(parent: RawNode | None, name: str, aid: str, role: str = "listitem") -> RawNode:
    """A fresh child node with a unique native id, for a scripted mutation."""
    base = 500_000
    if parent is not None and parent.native.runtime_id:
        base = parent.native.runtime_id[0] * 1000 + len(parent.children) + 1
    wid = parent.native.window_id if parent is not None else "w1"
    if role == "listitem":
        patterns = P_SELECT
    elif role in ("menuitem", "button"):
        patterns = P_INVOKE
    else:
        patterns = P_TEXT
    return RawNode(
        native=NativeRef(
            window_id=wid,
            runtime_id=(base,),
            automation_id=aid,
            path=(base,),
        ),
        role=role,
        name=name,
        patterns=patterns,
    )


def store_add(node: RawNode, root: RawNode) -> None:
    """Mirror the app's Add button: bump the cart, append a line, log it.

    The clicked row is ``node`` (the Add button), so the title/price are read
    from its own row - which is what makes one script serve all 240 rows.
    """
    import re

    row = _row_of(root, node)
    name = _row_title(row)
    price = _row_price(row)
    summary = _find(root, "store_cart_count")
    items = _find(root, "store_cart_items")
    record = _find(root, "store_last_action")
    if summary is not None and summary.name is not None:
        m = re.search(r"(\d+)\s*items", summary.name)
        t = re.search(r"\$(\d+\.\d{2})", summary.name)
        count = (int(m.group(1)) if m else 0) + 1
        total = (float(t.group(1)) if t else 0.0) + price
        summary.name = f"{count} items ${total:.2f}"
    if items is not None:
        items.children.append(
            _make_item(items, name, f"store_cart_item_{len(items.children)}")
        )
    if record is not None:
        record.name = f"已加入 {name}"


def store_clear_cart(node: RawNode, root: RawNode) -> None:
    """Mirror the app's confirmed checkout: empty the cart and log it.

    Takes ``node`` as well as ``root`` because every scripted mutator shares one
    signature (`(node, root)`); the checkout button is the node here and its row
    carries nothing the mutation needs.
    """
    summary = _find(root, "store_cart_count")
    items = _find(root, "store_cart_items")
    record = _find(root, "store_last_action")
    if summary is not None:
        summary.name = "0 items $0.00"
    if items is not None:
        items.children.clear()
    if record is not None:
        record.name = "已结算"


def store_search(node: RawNode, root: RawNode) -> None:
    """Mirror the app's Search button: filter the catalog and update the count."""
    box = _find(root, "store_search")
    products = _find(root, "store_products")
    summary = _find(root, "store_result_summary")
    query = (box.value or "") if box is not None else ""
    if products is not None and products.children:
        # Filtering rebuilds the list from the catalog, so the kept rows are the
        # ones whose title contains the query - the state a real search leaves.
        all_rows = list(products.children)
        if query:
            kept = [r for r in all_rows if query.casefold() in _row_title(r).casefold()]
            products.children = kept or [all_rows[0]]
        if summary is not None:
            summary.name = f"{len(products.children)} 件商品"


def form_submit(node: RawNode, root: RawNode) -> None:
    """Mirror the app's Submit: validate, then append a record."""
    name_box = _find(root, "form_name")
    email_box = _find(root, "form_email")
    agree = _find(root, "form_agree")
    validation = _find(root, "form_validation")
    record_id = _find(root, "form_id")
    records_list = _find(root, "form_records_list")
    if records_list is not None and records_list.children and all(
        c.name == "（尚无提交）" for c in records_list.children
    ):
        records_list.children.clear()
    if validation is None:
        return
    name = (name_box.value or "") if name_box is not None else ""
    agree_on = agree is not None and "checked" in agree.states
    if not name.strip():
        validation.name = "姓名不能为空"
        return
    if not agree_on:
        validation.name = "必须同意条款"
        return
    validation.name = "未提交"
    email = (email_box.value or "") if email_box is not None else ""
    if records_list is not None:
        seq = len(records_list.children) + 1
        records_list.children.append(
            _make_item(records_list, f"#{seq} {name} <{email}> 优先级=低",
                       f"form_record_{seq}")
        )
    if record_id is not None:
        count = len(records_list.children) if records_list is not None else 1
        record_id.value = f"REQ-{count + 1:04d}"


def form_reset(node: RawNode, root: RawNode) -> None:
    for aid in ("form_name", "form_email", "form_notes"):
        field = _find(root, aid)
        if field is not None:
            field.value = ""
    validation = _find(root, "form_validation")
    if validation is not None:
        validation.name = "未提交"
    agree = _find(root, "form_agree")
    if agree is not None:
        agree.states = frozenset(s for s in agree.states if s != "checked")


def chat_send(node: RawNode, root: RawNode) -> None:
    """Mirror the app's Send: append the message and a send-log line."""
    composer = _find(root, "chat_composer")
    messages = _find(root, "chat_messages")
    outbox_list = _find(root, "chat_outbox_list")
    title = _find(root, "chat_title")
    text = (composer.value or "").strip() if composer is not None else ""
    conversation = (title.name or "会话") if title is not None else "会话"
    if messages is not None:
        messages.children.append(
            _make_item(messages, text, f"chat_sent_{len(messages.children)}", role="text")
        )
    if outbox_list is not None:
        outbox_list.children.append(
            _make_item(
                outbox_list,
                f'{conversation}: 已发送 "{text}"',
                f"chat_outbox_{len(outbox_list.children)}",
            )
        )
    if composer is not None:
        composer.value = ""


def console_send(node: RawNode, root: RawNode) -> None:
    """Mirror the console app's Send: append to the thread, name it in status."""
    input_box = _find(root, "console_input")
    thread = _find(root, "console_thread")
    status = _find(root, "console_status")
    title = _find(root, "console_title")
    text = (input_box.value or "").strip() if input_box is not None else ""
    contact = (title.name or "会话") if title is not None else "会话"
    if thread is not None:
        thread.children.append(
            _make_item(thread, f"我: {text}",
                       f"console_thread_sent_{len(thread.children)}")
        )
    if status is not None:
        status.name = f'{contact}: 已发送 "{text}"'
    if input_box is not None:
        input_box.value = ""


def _conversation_rows(root: RawNode) -> list[RawNode]:
    box = _find(root, "chat_conversations")
    return list(box.children) if box is not None else []


def chat_select(node: RawNode, root: RawNode) -> None:
    """Mirror selecting a conversation: highlight it, swap the history."""
    rows = _conversation_rows(root)
    index = next(
        (i for i, r in enumerate(rows) if r is node),
        0,
    )
    for i, row in enumerate(rows):
        row.states = (
            frozenset(row.states | {"selected"})
            if i == index
            else frozenset(row.states - {"selected"})
        )
    title = _find(root, "chat_title")
    if title is not None:
        title.name = f"群组 {index + 1}"
    messages = _find(root, "chat_messages")
    if messages is not None:
        messages.children = [
            _make_item(
                messages, f"历史消息 {m + 1} @群组{index + 1}",
                f"chat_history_{index}_{m}", role="text",
            )
            for m in range(CHAT_HISTORY)
        ]


def console_select(node: RawNode, root: RawNode) -> None:
    """Mirror the app's SelectionChanged: highlight the row, swap the thread.

    Bound to the row's ``select``/``activate`` in :func:`console_scripts`, so
    choosing a contact from the list has the same visible effect the real
    ``ListBox.SelectionChanged`` handler produces: the title changes and the
    thread is replaced.
    """
    rows: list[RawNode] = []
    box = _find(root, "console_contacts")
    if box is not None:
        rows = list(box.children)
    index = next((i for i, r in enumerate(rows) if r is node), 0)
    for i, row in enumerate(rows):
        row.states = (
            frozenset(row.states | {"selected"})
            if i == index
            else frozenset(row.states - {"selected"})
        )
    title = _find(root, "console_title")
    if title is not None:
        title.name = f"联系人 {index + 1}"
    thread = _find(root, "console_thread")
    if thread is not None:
        thread.children = [
            _make_item(thread, f"（与 联系人 {index + 1} 的历史消息）",
                       f"console_thread_{index}_0", role="text")
        ]


def noop(node: RawNode, root: RawNode) -> None:
    """A control that really does nothing (used for buttons with no handler)."""


def menu_open(node: RawNode, root: RawNode) -> None:
    """Mirror opening a menu bar entry: realize that entry's items.

    Matches a real menu: the items are children of the menubar entry, and only
    the opened entry has them.
    """
    label = node.name or ""
    entry = _find(root, f"menu_bar_{label}")
    if entry is None:
        return
    items = _MENU_ITEMS.get(label, [])
    entry.children = [
        _make_item(entry, item_label, f"menu_item_{aid}", role="menuitem")
        for aid, item_label in items
    ]
    entry.states = frozenset({"expanded"})
    # Close the *other bar entries*. The menubar container is also a `menu` role
    # (the compiler aliases `menubar` to `menu`), so the discriminator is the
    # entry id prefix, not the role - collapsing by role alone would empty the
    # container and take every entry with it.
    for other in root.walk():
        aid = other.native.automation_id or ""
        if other is entry or not aid.startswith("menu_bar_"):
            continue
        other.states = frozenset({"collapsed"})
        other.children = []


def menu_item_action(node: RawNode, root: RawNode) -> None:
    """Mirror choosing a menu item: name it in the status line."""
    status = _find(root, "menu_status")
    if status is not None:
        status.name = f"已执行: {node.name or ''}"
    # Choosing an item closes its menu, as a real menu does.
    for entry in root.walk():
        if (entry.native.automation_id or "").startswith("menu_bar_"):
            entry.states = frozenset({"collapsed"})
            entry.children = []


def menu_context_open(node: RawNode, root: RawNode) -> None:
    """Mirror the runtime's context_menu gesture on the row."""
    if _find(root, "menu_ctx_rename") is not None:
        return
    root.children.append(
        _node(
            node.native.window_id, "menu", "上下文", ids=_Ids(),
            children=[
                _make_item(root, "重命名", "menu_ctx_rename", role="menuitem"),
                _make_item(root, "删除", "menu_ctx_delete", role="menuitem"),
            ],
        )
    )


def dialog_open(node: RawNode, root: RawNode) -> None:
    """Mirror pressing Delete: mount the confirmation dialog."""
    if _find(root, "dialog_confirm") is not None:
        return
    root.children.append(
        _node(
            node.native.window_id, "dialog", "确认删除", ids=_Ids(),
            states=frozenset({"modal"}),
            children=[
                _make_item(root, "确定要删除最后一项吗？", "dialog_message", role="text"),
                _make_item(root, "确定", "dialog_confirm", role="button"),
                _make_item(root, "取消", "dialog_cancel", role="button"),
            ],
        )
    )


def dialog_confirm(node: RawNode, root: RawNode) -> None:
    """Mirror confirming: remove the last row, close the dialog, say so."""
    rows = _find(root, "dialog_rows")
    if rows is not None and rows.children:
        rows.children.pop()
    status = _find(root, "dialog_status")
    if status is not None:
        status.name = "已删除"
    dialog = next((n for n in root.walk() if n.role == "dialog"), None)
    if dialog is not None:
        root.children.remove(dialog)


def dialog_cancel(node: RawNode, root: RawNode) -> None:
    """Mirror cancelling: close the dialog, change nothing else."""
    status = _find(root, "dialog_status")
    if status is not None:
        status.name = "已取消"
    dialog = next((n for n in root.walk() if n.role == "dialog"), None)
    if dialog is not None:
        root.children.remove(dialog)


#: Menu bar label -> its items, shared by the fixture and the opener mutator so
#: the two cannot drift.
_MENU_ITEMS: dict[str, list[tuple[str, str]]] = {
    "文件": [("new", "新建"), ("save", "保存"), ("exit", "退出")],
    "编辑": [("undo", "撤销"), ("redo", "重做"), ("copy", "复制")],
    "帮助": [("about", "关于")],
}


#: Mutators the ``callback`` mutation op can name. Kept as a registry so a test
#: cannot silently reference a mutator that does not exist: an unknown name is
#: a no-op only after the assertion in :func:`callbacks` has run.
SCENARIO_MUTATIONS: dict[str, Callable[[RawNode, RawNode], None]] = {
    "store_add": store_add,
    "store_clear_cart": store_clear_cart,
    "store_search": store_search,
    "form_submit": form_submit,
    "form_reset": form_reset,
    "chat_send": chat_send,
    "chat_select": chat_select,
    "console_select": console_select,
    "console_send": console_send,
    "menu_open": menu_open,
    "menu_item_action": menu_item_action,
    "menu_context_open": menu_context_open,
    "dialog_open": dialog_open,
    "dialog_confirm": dialog_confirm,
    "dialog_cancel": dialog_cancel,
    "noop": noop,
}


#: Mock action scripts, keyed by ``<name>:<primitive>``. A name that is
#: ambiguous across scenarios would make a script fire on the wrong window, so
#: each name here belongs to exactly one scenario.
def scenario_scripts() -> dict:
    """Scripted mutations for the Scenario Suite mock provider."""
    return {
        # store
        "加入:activate": [{"op": "callback", "fn": "store_add"}],
        "结算:activate": [{"op": "callback", "fn": "store_clear_cart"}],
        "搜索:activate": [{"op": "callback", "fn": "store_search"}],
        # form
        "提交:activate": [{"op": "callback", "fn": "form_submit"}],
        "重置:activate": [{"op": "callback", "fn": "form_reset"}],
        # chat
        "发送:activate": [{"op": "callback", "fn": "chat_send"}],
        # menu: the bar entries open their submenu; items act then close it.
        "文件:activate": [{"op": "callback", "fn": "menu_open"}],
        "编辑:activate": [{"op": "callback", "fn": "menu_open"}],
        "帮助:activate": [{"op": "callback", "fn": "menu_open"}],
        "新建:activate": [{"op": "callback", "fn": "menu_item_action"}],
        "保存:activate": [{"op": "callback", "fn": "menu_item_action"}],
        "撤销:activate": [{"op": "callback", "fn": "menu_item_action"}],
        "文档 1:context_menu": [{"op": "callback", "fn": "menu_context_open"}],
        # dialog: Delete opens the confirmation; its buttons act on it.
        "删除:activate": [{"op": "callback", "fn": "dialog_open"}],
        "确定:activate": [{"op": "callback", "fn": "dialog_confirm"}],
        "取消:activate": [{"op": "callback", "fn": "dialog_cancel"}],
        # console sends through its own button, which shares the label; the
        # opener is separate so both scenarios can be scripted at once without
        # a name collision firing the wrong mutation.
    }


def callbacks() -> dict:
    """The mutator registry, fresh per provider instance."""
    return dict(SCENARIO_MUTATIONS)


def chat_scripts() -> dict:
    """Scripts for the chat screen (send + conversation selection)."""
    scripts = scenario_scripts()
    scripts["发送:activate"] = [{"op": "callback", "fn": "chat_send"}]
    return scripts


def console_scripts(*, contacts: int = 10) -> dict:
    """Scripts for the console screen (send through its plain text box).

    The contact rows are keyed individually rather than by a shared name: the
    mock matches ``<node name>:<primitive>``, and every row has a different name
    ("联系人 1", "联系人 2", ...), so a single generic key could not address
    them - which is also the honest shape, since selecting row 5 and selecting
    row 7 are different actions with different effects.
    """
    scripts = scenario_scripts()
    scripts["发送:activate"] = [{"op": "callback", "fn": "console_send"}]
    for i in range(contacts):
        name = f"联系人 {i + 1}"
        # The row's default action: the app's ListBox SelectionChanged.
        scripts[f"{name}:select"] = [{"op": "callback", "fn": "console_select"}]
        scripts[f"{name}:activate"] = [{"op": "callback", "fn": "console_select"}]
        # The per-row opener button.
        scripts["打开:activate"] = [{"op": "callback", "fn": "noop"}]
    return scripts


# ---------------------------------------------------------------------------
# scenario registry: one entry per screen, for callers that iterate them
# ---------------------------------------------------------------------------

#: screen name -> (tree builder, script builder). The single source of truth for
#: "what screens exist", so the benchmark, the contract tests and any new test
#: all cover the same set.
SCREENS: dict[str, tuple[Callable[..., RawTree], Callable[[], dict]]] = {
    "store": (store_tree, scenario_scripts),
    "form": (form_tree, scenario_scripts),
    "chat": (chat_tree, chat_scripts),
    "console": (console_tree, console_scripts),
    "tree": (tree_tree, scenario_scripts),
    "native": (native_tree, scenario_scripts),
    "menu": (menu_tree, scenario_scripts),
    "dialog": (dialog_tree, scenario_scripts),
}


def make_provider(screen: str, **tree_kw):
    """A mock provider for one screen, with its scripts and mutators wired.

    Kept here rather than in each caller so a scenario is always set up the same
    way: the same scripts, the same callback registry, the same tree builder.
    A test that built its own provider could pass against a screen the app would
    never produce.
    """
    from somnus.providers.mock import MockProvider

    builder, scripts_factory = SCREENS[screen]
    return MockProvider(
        fixtures=[builder(**tree_kw)],
        scripts=scripts_factory(),
        callbacks=callbacks(),
    )


def resolve_anchors(session, anchors: dict[str, str]) -> dict[str, str]:
    """Resolve ``name -> automation_id`` into ``name -> ref`` on a live session.

    Automation ids are the stable handle (they do not move with the tree), so a
    caller that wants to address a control names it that way and resolves it
    once per observation. This is the same idea as the pack's ``named_refs``,
    expressed without needing a pack.
    """
    out: dict[str, str] = {}
    for name, automation_id in anchors.items():
        for node in session.result.root.walk():
            source = session.result.sources.get(node.ref)
            if source is not None and source.native.automation_id == automation_id:
                out[name] = node.ref
                break
    return out


def all_tree_ids(
    fanout: int = TREE_FANOUT, depth: int = TREE_DEPTH
) -> set[str]:
    """Every branch id of the tree scenario, i.e. the fully expanded tree."""
    ids: set[str] = set()
    frontier = ["tree_root"]
    for _ in range(depth):
        nxt: list[str] = []
        for prefix in frontier:
            ids.add(prefix)
            nxt.extend(f"{prefix}_{i}" for i in range(fanout))
        frontier = nxt
    return ids
