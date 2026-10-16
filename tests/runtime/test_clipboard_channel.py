"""剪贴板通道：给"没有可写接口"的自绘输入框写入文本，且不污染用户的剪贴板。

前提（见 docs/interface-capability-report.md 3.3、四节）：自绘输入框（QQ 的
ProseMirror、豆包的 tiptap、Chromium/Electron 的富文本）既没有 Value 模式，
也没有元素级 HWND，ValuePattern 和 EM_REPLACESEL 都够不着它们。够得着的
是 Ctrl+V——应用自己的 WM_PASTE 把剪贴板里的文本一次性读走，所以投递量与
文本长度无关（实测 10~3000 字符恒定 ~37ms），而逐字符 SendInput 会把 2N 个
事件塞进输入队列、被目标滞留。

代价是剪贴板属于用户，所以这条通道必须处理三件事，本文件逐个钉死：

1. **防泄漏**：写入时附带四条 opt-out 格式，实测历史 count 1 -> 0。
   这是本通道存在的正当性所在，不是可选装饰。
2. **防误伤**：剪贴板里是图片/文件时**拒绝运行**——恢复不了，实测
   {image: True} -> {image: False}。静默毁掉用户的截图比抢焦点更糟。
3. **不谎报**：延迟渲染（promise）在本环境拿不到读取信号（实测 OLE 抢先
   实体化），所以恢复只能靠定时，必须如实标注"未确认"。

同时守住通道定位：它排在两条无焦点通道**之后**、键盘**之前**；
默认关闭，需要 --allow-clipboard 显式开启。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

import pytest

from somnus.ir.capability import PrimitiveStep
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session

_runtime_counter = [0]


# ---------------------------------------------------------------------------
# 实机剪贴板辅助：造内容 + 收尾还原
# ---------------------------------------------------------------------------


def _machine_clipboard():
    """当前真实剪贴板的快照，用于测试收尾时还原。

    这些测试会真的动系统剪贴板，所以每条都必须把原状态放回去——
    测试自己也不应该污染开发者的剪贴板。
    """
    from somnus.providers.windows import clipboard

    return clipboard.snapshot()


def _restore_machine_clipboard(snap) -> None:
    from somnus.providers.windows import clipboard

    if snap is not None and snap.captured:
        clipboard.restore(snap)


def _seed_text_and_custom(text: str, custom_id, blob):
    """把文本（可加一个自定义格式）放进真剪贴板，返回放入前的快照。"""
    from somnus.providers.windows import clipboard

    baseline = clipboard.snapshot()
    user32 = clipboard._user32()
    kernel32 = clipboard._kernel32()
    with clipboard._clipboard_open(user32):
        user32.EmptyClipboard()
        clipboard._set_text(user32, kernel32, text)
        if custom_id and blob:
            handle = clipboard._alloc_block(kernel32, blob)
            user32.SetClipboardData(custom_id, wintypes.HANDLE(handle))
    return baseline


def _clipboard_with_bitmap():
    """放一张真位图进剪贴板，返回放入前的快照。"""
    from somnus.providers.windows import clipboard

    gdi32 = clipboard._gdi32()
    user32 = clipboard._user32()
    gdi32.CreateCompatibleBitmap.restype = wintypes.HANDLE
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]

    baseline = clipboard.snapshot()
    with clipboard._clipboard_open(user32):
        user32.EmptyClipboard()
        hdc = user32.GetDC(None)
        hbmp = gdi32.CreateCompatibleBitmap(hdc, 32, 24)
        user32.SetClipboardData(clipboard.CF_BITMAP, wintypes.HANDLE(hbmp))
        user32.ReleaseDC(None, hdc)
    return baseline


def _n(role, name=None, *, patterns=frozenset(), states=frozenset(), children=None, rid=None, cls=None):
    if rid is None:
        _runtime_counter[0] += 1
        rid = _runtime_counter[0]
    return RawNode(
        native=NativeRef(window_id="w1", runtime_id=(rid,), class_name=cls),
        role=role,
        name=name,
        states=states,
        patterns=patterns,
        children=children or [],
    )


def _tree(children, title="App"):
    return RawTree(
        window=WindowInfo(window_id="w1", title=title, process="app.exe", pid=1),
        root=_n("window", title, children=children),
    )


def _session(tree, **policy_kw):
    return Session(MockProvider(fixtures=[tree]), allow_packs=False, policy=Policy(**policy_kw))


def _self_drawn_editor():
    """QQ 的 ProseMirror 形状：没有 value、没有 HWND，只有 text。"""
    return _n("group", "editor", patterns=frozenset({"text"}), cls="ProseMirror")


# ---------------------------------------------------------------------------
# 通道定位：默认关闭，且排在无焦点通道之后
# ---------------------------------------------------------------------------


def test_clipboard_route_is_in_the_set_text_chain():
    """链里声明了剪贴板路线，排在 Value 写入之后（正是自绘输入框的形状）。"""
    session = _session(_tree([_self_drawn_editor()]), allow_clipboard=True)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")
    steps = [(s.primitive, s.condition) for s in node.get_cap("set_text").via]
    assert ("paste_text", None) in steps, steps


def test_clipboard_sits_after_the_focus_free_route():
    """粘贴排在 Value 写入之后：那条通道不碰用户的焦点和剪贴板。

    顺序不是风格问题——ValuePattern 让应用在自己的线程上完成写入，原子、
    不抢焦点、不借剪贴板。让粘贴排到它前面，等于每次都去动用户的剪贴板。
    """
    session = _session(_tree([_self_drawn_editor()]), allow_clipboard=True)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")
    primitives = [s.primitive for s in node.get_cap("set_text").via]
    assert primitives.index("paste_text") > primitives.index("set_text"), primitives


def test_clipboard_sits_before_typing():
    """粘贴排在键盘之前：同样要前台，但没有队列滞留，且长度无关。

    报告实测：粘贴 3000 字符 37ms；逐字符打字 100 字符要 3098ms 且只落 14 个。
    既然两条路都要抢焦点，没有理由先挑差的那条。
    """
    session = _session(_tree([_self_drawn_editor()]), allow_clipboard=True)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")
    primitives = [s.primitive for s in node.get_cap("set_text").via]
    assert primitives.index("paste_text") < primitives.index("type_text"), primitives


def test_clipboard_can_be_disabled_by_policy():
    """策略关掉剪贴板时，报错并说明开关，不偷偷用。"""
    session = _session(
        _tree([_self_drawn_editor()]), allow_clipboard=False, allow_keyboard=False
    )
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")

    out = session.dispatch(f'CALL {node.ref}.set_text("hello")')
    assert out.startswith("ERR"), out
    # 两条合成路线都被拒时，两条开关都要点到（链尾的键盘是主因）。
    assert "--allow-clipboard" in out, out
    assert "--allow-keyboard" in out, out
    # 每条被拒路线都带上自己的说明，而不是只给一个裸路线名。
    assert "paste_text" in out and "type_text" in out, out

    executed = [p for _r, p, _v in session.provider.executed]
    assert "paste_text" not in executed, executed


def test_a_disabled_clipboard_does_not_hide_the_keyboard_route():
    """剪贴板被拒时链继续走：不启用剪贴板不影响 --allow-keyboard 的可用性。

    每条路线各自授权（policy 按 step 检查）。若第一条被拒就终止，
    一个只开了键盘的宿主会拿到"没有可用接口"，而它的合法路线明明就在下一步。
    """
    session = _session(
        _tree([_self_drawn_editor()]), allow_keyboard=True, allow_clipboard=False
    )
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")

    out = session.dispatch(f'CALL {node.ref}.set_text("typed")')
    assert out.startswith("OK"), out
    executed = [p for _r, p, _v in session.provider.executed]
    assert "type_text" in executed, executed
    # 剪贴板没有被使用（它没被授权），但它的存在没有挡住键盘。
    assert "paste_text" not in executed, executed


def test_an_enabled_native_route_is_never_bypassed_for_the_clipboard():
    """有 Value 模式时就走 Value 模式，绝不退到剪贴板。"""
    field = _n("edit", "Name", patterns=frozenset({"value", "text"}))
    session = _session(_tree([field]), allow_clipboard=True)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")

    out = session.dispatch(f'CALL {node.ref}.set_text("x")')
    assert out.startswith("OK"), out
    executed = [p for _r, p, _v in session.provider.executed]
    assert executed == ["set_text"], executed


# ---------------------------------------------------------------------------
# opt-out：预防，而不是事后删除
# ---------------------------------------------------------------------------


def test_opt_out_formats_are_the_documented_four():
    """四条格式及其载荷形态，逐字对齐 Windows 的协议。

    CanIncludeInClipboardHistory / CanUploadToCloudClipboard 的值必须是
    DWORD 0（值才是开关），另两条只要求存在。拼错一个名字或写错载荷类型，
    防护就静默失效——所以这里钉死字符串本身，而不是"大致存在"。
    """
    from somnus.providers.windows.clipboard import OPT_OUT_FORMATS

    names = [n for n, _dword in OPT_OUT_FORMATS]
    assert names == [
        "ExcludeClipboardContentFromMonitorProcessing",
        "CanIncludeInClipboardHistory",
        "CanUploadToCloudClipboard",
        "ClipboardViewerIgnore",
    ], names
    by_name = dict(OPT_OUT_FORMATS)
    assert by_name["CanIncludeInClipboardHistory"] is True
    assert by_name["CanUploadToCloudClipboard"] is True
    # 只要求"存在"的两条不携带 DWORD。
    assert by_name["ExcludeClipboardContentFromMonitorProcessing"] is False
    assert by_name["ClipboardViewerIgnore"] is False


def test_write_reports_which_opt_out_formats_it_installed():
    """写入返回实际装上的 opt-out 格式，调用方能验证防护真的生效。

    返回而不是静默吞掉："写了文本但没标上"正是最危险的状态——
    它看起来和"剪贴板是干净的"一模一样。
    """
    from somnus.providers.windows import clipboard

    if not clipboard.available():
        pytest.skip("windows only")

    before = clipboard.snapshot()

    try:
        installed = clipboard.write_text("somnus-optout-probe")
        assert set(installed) == {n for n, _d in clipboard.OPT_OUT_FORMATS}, installed
        # 必须在**我们的写入还在剪贴板上时**检查：还原会正确地把这四条
        # 一起去掉（它们不属于用户的内容），所以事后检查必然是空的。
        live = clipboard.snapshot()
        for name, _dword in clipboard.OPT_OUT_FORMATS:
            cf = clipboard._user32().RegisterClipboardFormatW(name)
            assert cf in live.formats, f"{name} missing from {live.formats}"
    finally:
        # 不给用户的剪贴板留东西：完整还原（含图片/自定义格式）。
        clipboard.restore(before)


def test_a_failed_opt_out_is_reported_not_swallowed(monkeypatch):
    """装不上 opt-out 格式时如实返回空集，而不是假装成功。"""
    from somnus.providers.windows import clipboard

    if not clipboard.available():
        pytest.skip("windows only")

    monkeypatch.setattr(
        clipboard, "_set_opt_out", lambda user32, kernel32: []
    )
    before = clipboard.snapshot()
    try:
        installed = clipboard.write_text("somnus-optout-fail")
        assert installed == ()
    finally:
        clipboard.restore(before)


# ---------------------------------------------------------------------------
# 防误伤：剪贴板里是不可恢复的内容时，拒绝运行
# ---------------------------------------------------------------------------


def test_snapshot_captures_non_text_content_instead_of_refusing():
    """图片在剪贴板上时不再拒用，而是把它的字节一并快照下来。

    这是从 simply.clipboard-monitor 借鉴后的行为变更：以前只能重新发布纯文本，
    所以图片恢复不了，只能拒用；现在位图会被序列化成 CF_DIB 块，因此可以还原，
    也就没有理由再拒绝。has_non_text() 保留下来仅用于诊断。
    """
    from somnus.providers.windows import clipboard

    if not clipboard.available():
        pytest.skip("windows only")

    restricted = _clipboard_with_bitmap()
    try:
        snap = clipboard.snapshot()
    finally:
        _restore_machine_clipboard(restricted)

    assert snap.has_non_text() == "image"
    bitmap = next(
        (e for e in snap.entries if e.format_id == clipboard.CF_BITMAP), None
    )
    assert bitmap is not None, [e.name for e in snap.entries]
    assert bitmap.handle_type == clipboard.HANDLE_BITMAP
    # 捕获的是 CF_DIB 块（BITMAPINFOHEADER + 像素），不是空数据。
    assert bitmap.data and len(bitmap.data) > ctypes.sizeof(clipboard._BITMAPINFOHEADER)


def test_a_bitmap_format_reports_its_handle_type():
    """句柄形状决定读写策略：位图走 hbitmap，其它走 hglobal。"""
    from somnus.providers.windows import clipboard

    assert clipboard.handle_type_of(clipboard.CF_BITMAP) == clipboard.HANDLE_BITMAP
    assert (
        clipboard.handle_type_of(clipboard.CF_ENHMETAFILE)
        == clipboard.HANDLE_ENHMETAFILE
    )
    assert clipboard.handle_type_of(clipboard.CF_PALETTE) == clipboard.HANDLE_NONE
    assert clipboard.handle_type_of(clipboard.CF_UNICODETEXT) == clipboard.HANDLE_GLOBAL
    # 自定义格式（0xC000+）是普通的内存块。
    assert clipboard.handle_type_of(0xC123) == clipboard.HANDLE_GLOBAL


def test_clipboard_unavailable_is_reported_as_unsupported(monkeypatch):
    """环境拿不到剪贴板时必须表现为 unsupported，而不是失败。

    "这个环境不让我粘贴"和"这个元素收不了文本"是两件事：前者应该让链继续
    尝试下一条路线，后者才是真正的 ACTION_NOT_SUPPORTED。这里钉住 executor
    的那次转换，因为它就是决定"继续还是报死"的那一点。
    """
    import somnus.providers.windows.executor as executor_mod
    from somnus.providers.windows.clipboard import PasteOutcome

    session = _session(_tree([_self_drawn_editor()]))
    session.observe()

    class _Element:
        def SetFocus(self):
            pass

    def _unavailable(text, *, verify=None):
        return PasteOutcome(
            ok=False,
            error="CLIPBOARD_UNAVAILABLE",
            message="cannot open clipboard (locked session)",
        )

    monkeypatch.setattr(executor_mod, "paste_text", _unavailable)

    result = executor_mod.run_primitive(_Element(), "paste_text", "hello", object())

    assert result.ok is False
    assert result.unsupported is True, result
    # 错误码归一成 ACTION_NOT_SUPPORTED：这是“这条路线在此不可用”的信号，
    # session 靠它继续走链，而不是把整次调用判死。
    assert result.error == "ACTION_NOT_SUPPORTED", result
    assert "clipboard" in (result.message or "").lower(), result


def test_restoring_an_entry_puts_every_format_back():
    """完整还原：文本 + 自定义格式，字节级一致，且格式顺序保持。

    这是本模块的核心承诺。写入前 8 种格式、我们的写入只剩 5 种、
    恢复后又回到完整列表（实测），否则“借用剪贴板”就是不可接受的。
    """
    from somnus.providers.windows import clipboard

    if not clipboard.available():
        pytest.skip("windows only")

    custom = clipboard._user32().RegisterClipboardFormatW("SomnusTestFormat")
    baseline = _seed_text_and_custom("用户原文_PRESERVE", custom, b"BLOB-1234567890")
    try:
        before = clipboard.snapshot()
        before_names = [e.name for e in before.entries]
        before_bytes = {e.name: e.data for e in before.entries if e.data is not None}

        # 模拟一次粘贴：清空并写入我们的 payload。
        clipboard.write_text("SOMNUS_PAYLOAD")
        assert clipboard.snapshot().text == "SOMNUS_PAYLOAD"

        failed = clipboard.restore(before)
        assert failed == (), failed

        after = clipboard.snapshot()
        assert [e.name for e in after.entries] == before_names
        assert after.text == "用户原文_PRESERVE"
        for name, data in before_bytes.items():
            got = next((e.data for e in after.entries if e.name == name), None)
            assert got == data, f"{name} bytes changed"
        # 我们的 payload 不能残留。
        assert "SOMNUS_PAYLOAD" not in (after.text or "")
    finally:
        _restore_machine_clipboard(baseline)


def test_restore_does_not_re_apply_the_opt_out_marks():
    """还原时不能把我们自己的 opt-out 标记写回用户的条目。

    那四条格式是“我们写入时”的标记，不属于用户的内容。若还原时一并写回，
    用户原本的条目会带上“不要进历史”的标记——这是对他内容的篡改。
    """
    from somnus.providers.windows import clipboard

    if not clipboard.available():
        pytest.skip("windows only")

    baseline = _seed_text_and_custom("OPT_OUT_CHECK", None, None)
    try:
        before = clipboard.snapshot()
        clipboard.write_text("SOMNUS_PAYLOAD")  # 这一步会装上四条 opt-out
        clipboard.restore(before)

        after = clipboard.snapshot()
        opt_out_ids = {
            clipboard._user32().RegisterClipboardFormatW(name)
            for name, _d in clipboard.OPT_OUT_FORMATS
        }
        leftover = opt_out_ids & set(after.formats)
        assert leftover == set(), f"opt-out marks left on the restored entry: {leftover}"
    finally:
        _restore_machine_clipboard(baseline)


# ---------------------------------------------------------------------------
# 不谎报：恢复是定时的，就必须说"未确认"
# ---------------------------------------------------------------------------


def test_paste_reports_restore_as_unconfirmed_when_there_is_no_read_signal():
    """拿不到读取信号时，恢复只能靠定时，返回值不许声称已确认。

    延迟渲染（promise）本该提供"目标已读取"的信号，但实测在本环境不可用：
    OLE 会在发布后 ~3-5ms 抢先实体化承诺，且无人索取，于是目标真正粘贴时
    不再产生第二次 WM_RENDERFORMAT（见 docs/interface-capability-report.md 3.3）。
    诚实标注是唯一正确选项。
    """
    from somnus.providers.windows.clipboard import PasteOutcome

    outcome = PasteOutcome(
        ok=True,
        restored=False,
        delivered=False,
        message="restore unconfirmed (no read signal available in this environment)",
    )
    assert "unconfirmed" in (outcome.message or "")


def test_delivery_wait_returns_false_without_a_verifier():
    """没有可读回的值时，投递等待如实返回 False（=在靠定时器猜）。"""
    from somnus.providers.windows import clipboard

    assert clipboard._await_delivery(None, "text") is False


# ---------------------------------------------------------------------------
# 通道被拒后，错误里要能看出试过什么
# ---------------------------------------------------------------------------


def test_all_synthetic_routes_refused_names_both_flags():
    """两个开关都关时，错误里同时点出剪贴板和键盘两条被拒的路线。"""
    session = _session(
        _tree([_self_drawn_editor()]), allow_clipboard=False, allow_keyboard=False
    )
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")

    out = session.dispatch(f'CALL {node.ref}.set_text("hello")')
    assert out.startswith("ERR"), out
    # 链尾（键盘）的拒绝是主因，同时列出被拒的全部路线及其说明。
    assert "keyboard" in out.lower(), out
    assert "refused routes" in out, out
    assert "paste_text" in out and "type_text" in out, out


def test_paste_step_falls_through_when_its_primitive_is_unsupported():
    """元素级拒绝（unsupported）也会继续走链，和原生路线一致。"""
    editor = _self_drawn_editor()
    session = _session(_tree([editor]), allow_clipboard=True, allow_keyboard=True)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")

    real_supports = MockProvider._supports
    MockProvider._supports = staticmethod(
        lambda n, prim: False if prim == "paste_text" else real_supports(n, prim)
    )
    try:
        out = session.dispatch(f'CALL {node.ref}.set_text("hello")')
    finally:
        MockProvider._supports = staticmethod(real_supports)

    assert out.startswith("OK"), out
    executed = [p for _r, p, _v in session.provider.executed]
    assert executed[0] == "paste_text", executed  # 先试了粘贴
    assert "type_text" in executed, executed  # 被拒后落到键盘


def test_paste_text_is_not_a_capability_the_llm_can_call():
    """paste_text 只是链里的一步路由细节，绝不作为能力暴露给 LLM。"""
    session = _session(_tree([_self_drawn_editor()]), allow_clipboard=True)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")
    assert "paste_text" not in node.cap_names(), node.cap_names()
