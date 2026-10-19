"""Fast-path router: deterministic utterances answered without the LLM."""

from __future__ import annotations

import json

from somnus.runtime import fastpath


def test_launch_rule_parses_common_chinese_shapes():
    for text, want in [
        ("打开微信", "微信"),
        ("帮我打开 闲鱼吧", "闲鱼"),
        ("启动 chrome", "chrome"),
        ("open taobao", "taobao"),
        ("打开 bilibili。", "bilibili"),
    ]:
        m = fastpath._LAUNCH_RE.match(text)
        assert m is not None, text
        assert m.group("target") == want, (text, m.group("target"))


def test_non_open_utterances_do_not_match():
    for text in ["搜索手机壳", "给妈妈发消息", "把这条消息置顶", ""]:
        assert fastpath._LAUNCH_RE.match(text) is None, text


def test_remember_only_caches_system_level_first_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(fastpath, "_CACHE_PATH", tmp_path / "fastpath.json")

    # A CALL touches live tree state - must not be replayed from memory.
    fastpath.remember("把消息置顶", "CALL tx1.context_menu()")
    assert fastpath._load_cache() == {}

    # Reaching a screen is state-free and replays fine.
    fastpath.remember("打开淘宝", "APP open taobao://home")
    fastpath.remember("去桌面", "SYSTEM home")
    cache = fastpath._load_cache()
    assert cache["打开淘宝"] == "APP open taobao://home"
    assert cache["去桌面"] == "SYSTEM home"


def test_try_route_replays_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(fastpath, "_CACHE_PATH", tmp_path / "fastpath.json")
    fastpath.remember("打开淘宝", "APP open taobao://home")

    class FakeProvider:
        def list_apps(self):
            return [{"package": "com.taobao.taobao", "label": "淘宝"}]

    class FakeSession:
        provider = FakeProvider()

        def __init__(self):
            self.dispatched = []

        def dispatch(self, text):
            self.dispatched.append(text)
            return "OK app=taobao://home rev=2\n~ refs discarded; OBSERVE"

    session = FakeSession()
    out = fastpath.try_route("打开淘宝", session)
    assert out is not None and "fastpath" in out
    assert session.dispatched == ["APP open taobao://home"]


def test_try_route_returns_none_for_unknown_utterance(tmp_path, monkeypatch):
    monkeypatch.setattr(fastpath, "_CACHE_PATH", tmp_path / "fastpath.json")

    class FakeProvider:
        def list_apps(self):
            return []

    class FakeSession:
        provider = FakeProvider()

    assert fastpath.try_route("把闲鱼上我的宝贝降价", FakeSession()) is None
