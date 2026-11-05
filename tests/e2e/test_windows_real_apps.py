"""Windows E2E against real desktop applications.

The scenario suite (``apps/scenario-suite``) is the controlled fixture for the
platform shapes. This file drives applications the user actually has: the live
Windows 11 Calculator (UWP, Chinese UI), Notepad, Character Map and Paint.
Nothing here clicks by coordinate or reads a screenshot - a step that cannot be
expressed as a capability on a printed ref is reported as an architecture
limit, not worked around.

Each test launches its own process(es) and cleans up only the processes it
started. Applications that were already running before the launch are treated
as the user's and make the test skip rather than take focus from them.

Run with::

    pytest -s -m windows tests/e2e/test_windows_real_apps.py
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.windows

#: A live application's first paint plus UIA registration is not instant.
_WINDOW_TIMEOUT = 25.0

#: ``calc.exe`` is only the activation launcher; the UWP window belongs to
#: ``CalculatorApp.exe``, which is what UIA reports as the window process.
_CALC_IMAGES = ["CalculatorApp.exe"]
_NOTEPAD_IMAGES = ["Notepad.exe", "notepad.exe"]
_PAINT_IMAGES = ["mspaint.exe"]
_CHARMAP_IMAGES = ["charmap.exe"]

#: `FOUND`/observation lines: ``ROLE ref "name"``. Only the first three tokens
#: are needed, the rest of the line is the capability list.
_NODE_RE = re.compile(r'^\s*(?P<role>[A-Z]+)\s+(?P<ref>[a-z]+\d+)\s+"(?P<name>[^"]*)"', re.M)
_WINDOW_PAIR_RE = re.compile(r'(w\d+) "([^"]*)"')


# ---------------------------------------------------------------------------
# process bookkeeping (launch / clean up our own apps only)
# ---------------------------------------------------------------------------


def _require_windows() -> None:
    if not sys.platform.startswith("win"):
        pytest.skip("windows only")


def _image_pids(images: list[str]) -> set[int]:
    """PIDs currently running under any of *images* (case-insensitive)."""
    pids: set[int] = set()
    for image in images:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001 - bookkeeping only
            continue
        for row in csv.reader(io.StringIO(out.stdout)):
            if len(row) >= 2 and row[1].strip().isdigit():
                pids.add(int(row[1]))
    return pids


def _kill_pids(pids: set[int]) -> None:
    for pid in sorted(pids):
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001 - teardown
            pass


@pytest.fixture
def apps(wait_for_window):
    """Launch live applications and guarantee only those are cleaned up.

    Modelled on the scenario fixture in ``test_windows_scenarios.py``: each
    launch waits for the window to appear, then returns a session bound to it.
    A second application can join the same session (``session=``) so the desktop
    summary and window switching are exercised over one session.
    """
    _require_windows()
    from somnus.providers.base import get_provider
    from somnus.runtime.session import Session

    sessions: list[Session] = []
    procs: list[subprocess.Popen] = []
    own_pids: list[tuple[list[str], set[int]]] = []

    def launch(
        exe: str,
        *,
        images: list[str],
        process: str,
        title: str | None = None,
        allow: list[str] | None = None,
        session: Session | None = None,
        timeout: float = _WINDOW_TIMEOUT,
    ) -> tuple[Session, object]:
        before = _image_pids(images)
        if before:
            pytest.skip(
                f"{images} already running (pids={sorted(before)}); "
                "refusing to steal or close a window this test did not start"
            )
        own_pids.append((images, before))
        if session is None:
            provider = get_provider("uia", allow_list=allow or images)
            session = Session(provider, allow_packs=False)
            sessions.append(session)
        proc = subprocess.Popen([exe])
        procs.append(proc)

        # UIA publishes the window after the HWND exists and after it is shown;
        # the shared wait polls for an allow-listed, visible window and fails
        # with desktop diagnostics instead of hiding the race behind a sleep.
        window = wait_for_window(
            session,
            process=process,
            title=title,
            timeout=timeout,
            proc=proc,
        )
        session.observe()
        return session, window

    try:
        yield SimpleNamespace(launch=launch)
    finally:
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - teardown
                pass
        for proc in procs:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - teardown
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001 - teardown
                    pass
        for images, before in own_pids:
            _kill_pids(_image_pids(images) - before)


@pytest.fixture(autouse=True)
def _wall_clock(request):
    """Print each case's wall time. Recorded, never used as a gate."""
    started = time.monotonic()
    yield
    print(f"\n[wall] {request.node.name}: {time.monotonic() - started:.2f}s")


# ---------------------------------------------------------------------------
# ref / node helpers
# ---------------------------------------------------------------------------


def _first_ref(text: str, *, role: str | None = None, name: str | None = None) -> str | None:
    for match in _NODE_RE.finditer(text):
        if role is not None and match.group("role") != role:
            continue
        if name is not None and match.group("name") != name:
            continue
        return match.group("ref")
    return None


def _summary(text: str) -> dict[str, str]:
    """Ref -> title from the observation's ``WINDOWS`` line."""
    line = next((ln for ln in text.splitlines() if ln.startswith("WINDOWS ")), None)
    return dict(_WINDOW_PAIR_RE.findall(line)) if line else {}


def _node(session, *, automation_id: str | None = None, name: str | None = None, role: str | None = None):
    for node in session.result.root.walk():
        if automation_id is not None:
            source = session.result.sources.get(node.ref)
            if source is None or source.native.automation_id != automation_id:
                continue
        if name is not None and (node.name or "") != name:
            continue
        if role is not None and node.role != role:
            continue
        return node
    return None


def _call(session, ref: str, capability: str) -> str:
    out = session.dispatch(f"CALL {ref}.{capability}()")
    assert not out.startswith("ERR"), out
    return out


def _ref_for(session, *, name: str, role: str | None = None) -> str | None:
    """The ref of an element by exact printed name.

    The observation is viewport-scoped: a row that is on screen but folded out
    of the projection can be absent from ``session.result`` while still
    reachable. FIND is the tool that widens the read, so it is the fallback -
    the same move a model would make from the observation's ``+N more`` note.
    """
    node = _node(session, name=name, role=role)
    if node is not None:
        return node.ref
    text = session.find(name)
    return _first_ref(text, role=role.upper() if role else None, name=name)


# ---------------------------------------------------------------------------
# Calculator helpers (Chinese Windows 11 UI)
# ---------------------------------------------------------------------------


def _calc_header(session) -> str:
    node = _node(session, automation_id="Header")
    return (node.name or "") if node is not None else ""


def _calc_display(session) -> str:
    """The result line ("显示为 0"). Falls back to FIND when the viewport read
    did not compile it."""
    node = _node(session, automation_id="CalculatorResults")
    if node is not None:
        return node.name or ""
    match = re.search(r'TEXT\s+\S+\s+"(显示为 [^"]*)"', session.find("显示为"))
    return match.group(1) if match else ""


def _calc_nav(session):
    return _node(session, automation_id="TogglePaneButton")


def _press(session, name: str) -> str:
    """Activate one Calculator button by its printed (Chinese) name."""
    ref = _ref_for(session, name=name, role="button")
    assert ref is not None, (
        f"button {name!r} is not in the current observation; "
        f"display={_calc_display(session)!r}\n{session.find(name)}"
    )
    return _call(session, ref, "activate")


def _select_mode(session, label: str) -> None:
    """Pick a Calculator mode from the navigation pane ("标准" / "科学")."""
    nav = _calc_nav(session)
    if nav is None:
        session.observe()
        nav = _calc_nav(session)
    assert nav is not None, "the calculator navigation button is not in the tree"
    if "打开" in (nav.name or ""):
        _call(session, nav.ref, "activate")
        session.observe()
    deadline = time.monotonic() + 5.0
    item = _ref_for(session, name=label, role="listitem")
    while item is None and time.monotonic() < deadline:
        time.sleep(0.2)
        session.observe()
        item = _ref_for(session, name=label, role="listitem")
    assert item is not None, session.find(label)
    _call(session, item, "select")
    session.observe()


def _ensure_standard(session) -> None:
    """Put a freshly launched Calculator in standard mode with the pane closed.

    Calculator persists the selected mode (and an unsaved Notepad buffer does
    the same for Notepad), so a test may inherit the previous run's desktop
    state. This resets it through the UI, using only printed refs.
    """
    session.observe()
    for _ in range(3):
        if "标准" in _calc_header(session):
            break
        _select_mode(session, "标准 计算器")
    for _ in range(3):
        nav = _calc_nav(session)
        if nav is None or "关闭" not in (nav.name or ""):
            break
        _call(session, nav.ref, "activate")
        session.observe()
    session.observe()
    assert "标准" in _calc_header(session), (
        f"calculator did not return to standard mode: {_calc_header(session)!r}"
    )
    nav = _calc_nav(session)
    assert nav is not None and "关闭" not in (nav.name or ""), (
        "the navigation pane should be closed for a deterministic baseline"
    )


# ---------------------------------------------------------------------------
# 1. Calculator: the fixed arithmetic flow
# ---------------------------------------------------------------------------


def test_calculator_does_7_times_8_with_a_real_key_sequence(apps):
    """Clear, then press 七 乘以 八 等于, and read 56 off the display.

    The buttons are named by what this Chinese Windows build prints
    ("七"/"乘以"/"八"/"等于"/"清除"), not by an English label table.
    """
    session, _ = apps.launch(
        "calc.exe",
        images=_CALC_IMAGES,
        process="calculatorapp.exe",
        title="计算器",
    )
    _ensure_standard(session)

    # The task starts with Clear. "清除" is one of three hits (the memory
    # clear variants come first), so the button is selected by exact name.
    _press(session, "清除")
    session.observe()
    assert _calc_display(session) == "显示为 0", _calc_display(session)

    for name in ("七", "乘以", "八", "等于"):
        out = _press(session, name)
        assert out.startswith("OK"), out

    session.observe()
    display = _calc_display(session)
    assert display == "显示为 56", (
        f"display reads {display!r} instead of 56; "
        f"expression={(_node(session, automation_id='CalculatorExpression').name if _node(session, automation_id='CalculatorExpression') else None)!r}"
    )
    expression = _node(session, automation_id="CalculatorExpression")
    assert expression is not None and "7 × 8" in (expression.name or ""), (
        expression.name if expression is not None else "expression node missing"
    )
    print(f"\n[calc] {expression.name!r} -> {display!r}")


# ---------------------------------------------------------------------------
# 2. Calculator: panel switch changes the layout and the refs
# ---------------------------------------------------------------------------


def test_calculator_switches_to_scientific_and_reports_ref_changes(apps):
    """Standard -> Scientific: new buttons appear, and moved refs are healed.

    Observed facts on this build (asserted below):

    * the mode item is a LISTITEM whose ``activate`` is refused by UIA
      (``ACTION_NOT_SUPPORTED``); ``select`` is the route that works;
    * the digit pad is re-parented by the switch, so its old fingerprint no
      longer matches. The registry relocates the same ``七`` by its last-seen
      role/name/index evidence and the call returns ``OK`` with a ``~ healed``
      note naming the new ref - it does not dead-end at STALE_TARGET;
    * an element that survives the switch (the result display, automation id
      ``CalculatorResults``) keeps its old ref and still resolves;
    * a scientific-only button with no standard-mode counterpart has nowhere
      to relocate to, so the switch back keeps the honest STALE_TARGET error.
    """
    session, _ = apps.launch(
        "calc.exe",
        images=_CALC_IMAGES,
        process="calculatorapp.exe",
        title="计算器",
    )
    _ensure_standard(session)
    obs = session.observe()

    science_only = ("三角学", "反函数", "分数", "π", "左括号", "指数", "模数", "Euler 数字")
    for name in science_only:
        assert session.find(name).startswith("FOUND 0"), (
            f"{name!r} exists in standard mode already:\n{session.find(name)}"
        )

    seven_ref = _ref_for(session, name="七", role="button")
    display_text = _calc_display(session)
    display_ref = _ref_for(session, name=display_text, role="text") if display_text else None
    assert seven_ref is not None and display_ref is not None, obs.text
    old_seven, old_display = seven_ref, display_ref

    _select_mode(session, "科学 计算器")
    # The default (folded) observation of this UWP window is viewport-scoped
    # and came back as the title-bar skeleton only (164 tokens); `full` renders
    # the whole compiled panel and is what proves the new buttons are visible.
    obs = session.observe(full=True)
    assert "科学" in _calc_header(session), _calc_header(session)
    for name in science_only:
        assert name in obs.text, f"{name!r} is not visible in the scientific observation:\n{obs.text}"

    science_only_ref = _first_ref(obs.text, role="BUTTON", name="三角学")
    assert science_only_ref, f"scientific-only button missing from observation:\n{obs.text}"

    # The re-parented digit: the old ref no longer resolves by fingerprint, but
    # the last-seen locator finds the same 七 and the call really runs.
    healed = session.dispatch(f"CALL {old_seven}.activate()")
    assert healed.startswith(("OK", "OK_UNVERIFIED")), healed
    assert "~ healed " in healed and "(via " in healed, healed
    match = re.search(r"~ healed (\S+)\u2192(\S+)", healed)
    assert match is not None, healed
    assert match.group(1) == old_seven, healed
    new_seven = match.group(2)
    assert new_seven != old_seven, healed

    # The healed ref must be a real, currently addressable element.
    session.observe()
    assert session.result.find(new_seven) is not None, healed

    # A ref whose native element survived the switch remains usable.
    survived = session.dispatch(f"CALL {old_display}.focus()")
    assert survived.startswith("OK"), survived
    print(
        f"\n[switch] seven {old_seven} -> {healed.splitlines()[0]} "
        f"[{match.group(0)}]; display {old_display} -> {survived.splitlines()[0]}; "
        f"full_observe={obs.tokens} tok, new ref {science_only_ref}"
    )

    # Switching back: the scientific-only button has no standard-mode
    # counterpart to relocate to, so this is the honest STALE failure branch.
    _select_mode(session, "标准 计算器")
    session.observe()
    assert "标准" in _calc_header(session), _calc_header(session)
    stale_back = session.dispatch(f"CALL {science_only_ref}.activate()")
    assert stale_back.startswith("ERR STALE_TARGET"), stale_back
    assert _ref_for(session, name="七", role="button") is not None, "the digit pad did not come back"


# ---------------------------------------------------------------------------
# 3. Notepad: write multi-line text and read it back
# ---------------------------------------------------------------------------


def test_notepad_writes_multiline_text_and_reads_it_back(apps):
    """A fresh Notepad window, written through set_text and read back.

    Notepad's editor is a native RichEditD2DPT: it publishes a Value pattern,
    so the write goes through ``set_text`` directly (audit log proves the
    primitive). That is the contrast with the self-drawn composer in
    ``test_windows_scenarios.py``, which has no Value pattern and needs the
    clipboard/keyboard chain. Windows 11 Notepad may restore one saved tab, so
    the blank "无标题" window is selected explicitly.
    """
    session, _ = apps.launch(
        "notepad.exe",
        images=_NOTEPAD_IMAGES,
        process="notepad.exe",
    )
    obs = session.observe()
    summary = _summary(obs.text)
    # The session's allow-list is Notepad's image only, so every window the
    # observation can name is a Notepad window. Filtering on the *title* is not
    # safe: Windows 11 restores saved tabs, and the observation abbreviates a
    # long title with "…" before the app name ("… - Notepad" becomes
    # "… - No…"), which made this test fail on a desktop with restored tabs.
    titles = dict(summary)
    assert titles, obs.text
    blank = [ref for ref, title in titles.items() if "无标题" in title]
    target = blank[0] if blank else next(iter(titles))
    out = session.dispatch(f"OBSERVE {target}")
    assert not out.startswith("ERR"), out
    assert "Notepad" in out or "记事本" in out, out

    editor = _node(session, name="文本编辑器", role="input")
    assert editor is not None, session.observe().text
    steps = [(step.primitive, step.condition) for step in editor.get_cap("set_text").via]
    assert steps[0] == ("set_text", "has:value"), steps

    text = "第一行\n第二行\n第三行"
    out = session.dispatch(f'CALL {editor.ref}.set_text("{text}")')
    assert not out.startswith("ERR"), out

    entry = session.audit_log()[-1]
    assert entry["primitive"] == "set_text", entry

    session.observe()
    editor = _node(session, name="文本编辑器", role="input")
    assert editor is not None, "the editor disappeared after the write"
    value = (editor.value or "").replace("\r", "\n")
    assert value == text, f"editor holds {value!r}, expected {text!r}"
    print(
        f"\n[notepad] window {target!r}, {value.count(chr(10)) + 1} lines, "
        f"primitive={entry['primitive']}"
    )


# ---------------------------------------------------------------------------
# 4. Two real windows: summary, active, OBSERVE / focus, refs across the switch
# ---------------------------------------------------------------------------


def test_two_real_windows_are_listed_addressable_and_refs_survive_switching(apps):
    """Character Map + Paint: the desktop summary must describe both.

    Regression target: registry refs are window-scoped and map one-to-one, so
    an input ref taken in one window must still resolve after the session
    switches away and back.
    """
    allow = _CHARMAP_IMAGES + _PAINT_IMAGES
    session, _ = apps.launch(
        "charmap.exe",
        images=_CHARMAP_IMAGES,
        allow=allow,
        process="charmap.exe",
        title="字符映射表",
    )
    session, _ = apps.launch(
        "mspaint.exe",
        images=_PAINT_IMAGES,
        session=session,
        allow=allow,
        process="mspaint.exe",
        title="画图",
    )

    obs = session.observe()
    summary = _summary(obs.text)
    assert len(summary) == 2, obs.text
    summary_line = next(ln for ln in obs.text.splitlines() if ln.startswith("WINDOWS "))
    charmap_ref = next((ref for ref, title in summary.items() if "字符映射表" in title), None)
    paint_ref = next((ref for ref, title in summary.items() if "画图" in title), None)
    assert charmap_ref is not None and paint_ref is not None, obs.text
    assert len(set(summary.values())) == 2, summary

    # OBSERVE wN really targets the named window.
    out = session.dispatch(f"OBSERVE {charmap_ref}")
    assert not out.startswith("ERR"), out
    assert f'WINDOW {charmap_ref} "字符映射表"' in out, out
    session.refresh_windows()
    assert session._window_by_ref(charmap_ref).active, (
        f"{charmap_ref} should be the foreground window after OBSERVE"
    )

    editor = _node(session, name="复制字符(A):", role="input")
    assert editor is not None, out
    editor_ref = editor.ref

    # CALL wN.focus() switches for real, and the other window becomes active.
    out = session.dispatch(f"CALL {paint_ref}.focus()")
    assert out.startswith("OK"), out
    assert f'WINDOW {paint_ref} "无标题 - 画图"' in out or f"WINDOW {paint_ref}" in out, out
    session.refresh_windows()
    assert session._window_by_ref(paint_ref).active, (
        f"{paint_ref} should be the foreground window after focus; "
        f"windows={[(w.window_id, w.title, w.active) for w in session.windows]}"
    )
    assert not session._window_by_ref(charmap_ref).active, (
        "Character Map should no longer be foreground"
    )
    print(
        f"\n[windows] {summary_line}; "
        f"{paint_ref} active={session._window_by_ref(paint_ref).active}"
    )

    # Switch back through the ref the first observation printed; the editor
    # ref from before the switch must still be valid.
    out = session.dispatch(f"CALL {charmap_ref}.focus()")
    assert out.startswith("OK"), out
    out = session.dispatch(f'CALL {editor_ref}.set_text("AB")')
    assert not out.startswith("ERR"), out

    session.observe()
    editor = _node(session, name="复制字符(A):", role="input")
    assert editor is not None and (editor.value or "").replace("\r", "") == "AB", (
        f"editor holds {getattr(editor, 'value', None)!r} after switching away and back"
    )


# ---------------------------------------------------------------------------
# 5. run: one script drives open -> AWAIT -> select
# ---------------------------------------------------------------------------


def test_run_opens_the_navigation_pane_and_selects_science_in_one_call(apps):
    """``run`` does CALL -> AWAIT -> CALL with one protocol round trip.

    Two separate calls plus a FIND wait would be three round trips (call open,
    find/wait, call select). Here it is one ``run`` (and the runtime does the
    settle between the steps itself).
    """
    session, _ = apps.launch(
        "calc.exe",
        images=_CALC_IMAGES,
        process="calculatorapp.exe",
        title="计算器",
    )
    _ensure_standard(session)
    session.observe()
    nav = _calc_nav(session)
    assert nav is not None and "打开" in (nav.name or ""), (
        f"expected a closed navigation pane, got {getattr(nav, 'name', None)!r}"
    )

    from somnus.sui.parse import parse_script

    started = time.monotonic()
    out = session.run_script(
        parse_script(
            f"CALL {nav.ref}.activate()\n"
            'AWAIT LISTITEM "科学 计算器" timeout=3\n'
            "CALL $it.select()"
        )
    )
    elapsed = time.monotonic() - started
    assert out.startswith("RUN OK steps=3/3"), out
    assert "AWAIT LISTITEM" in out and "-> ok ($it=" in out, out

    session.observe()
    assert "科学" in _calc_header(session), _calc_header(session)
    assert _ref_for(session, name="三角学", role="button") is not None, (
        "the scientific panel is not visible after the run:\n" + session.observe().text
    )
    # Recorded for the report: equivalent separate flow = CALL + FIND + CALL.
    print(f"\n[roundtrip] run=1, separate CALL+FIND+CALL=3, saved=2 ({elapsed:.2f}s)")


# ---------------------------------------------------------------------------
# 6. FIND(timeout=) waiting for a late element
# ---------------------------------------------------------------------------


def test_find_waits_for_the_display_to_change_after_a_late_key_press(apps):
    """The result text changes 1.2s after the search starts; find waits for it.

    The key is pressed *outside* the settled call path (a direct provider
    gesture), which is the "it appeared a moment later" shape a wait exists
    for. The display is an element in the main window, so a viewport read -
    what the wait polls - can see it.
    """
    session, _ = apps.launch(
        "calc.exe",
        images=_CALC_IMAGES,
        process="calculatorapp.exe",
        title="计算器",
    )
    _ensure_standard(session)
    session.observe()
    assert _calc_display(session) == "显示为 0", _calc_display(session)
    assert session.find("显示为 7").startswith("FOUND 0"), "the display already reads 7"

    seven_ref = _ref_for(session, name="七", role="button")
    assert seven_ref is not None, "the digit 七 is not in the standard pad"
    source = session.result.sources.get(seven_ref)
    assert source is not None, "the digit lost its native source after FIND"
    native = source.native

    results: list[object] = []

    def press_later() -> None:
        time.sleep(1.2)
        results.append(session.provider.execute(native, "activate"))

    worker = threading.Thread(target=press_later, daemon=True)
    started = time.monotonic()
    worker.start()
    out = session.find("显示为 7", timeout_s=1.5)
    elapsed = time.monotonic() - started
    worker.join(timeout=5)

    assert results and results[0].ok, f"the delayed key press failed: {results}"
    assert out.startswith("FOUND ") and '"显示为 7"' in out, out
    # The element appeared at 1.2s; the hit must land after that and before the
    # 1.5s deadline (one poll of overshoot allowed).
    assert 1.0 <= elapsed <= 1.8, (
        f"wait returned after {elapsed:.2f}s for a deadline of 1.5s; out:\n{out}"
    )
    print(f"\n[find] elapsed={elapsed:.2f}s deadline=1.5s -> {out.splitlines()[0]}")
    session.observe()
    assert _calc_display(session) == "显示为 7", _calc_display(session)
