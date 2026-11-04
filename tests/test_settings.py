"""Runtime timing settings: keys, precedence, and the ``configure`` tool.

The settle knobs are the one place where a host (or the model) is allowed to
change the runtime's pace. This file pins the contract:

1. the built-in defaults are the ones the optimisation plan names;
2. precedence is default < file < env < runtime, in that order;
3. a bad value is refused, never coerced (``"40"`` is not the number 40) and a
   runtime patch is all-or-nothing;
4. ``configure`` echoes the values actually in force, and ``persist=true``
   writes the same file ``config.load()`` reads.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from somnus import config as config_module


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """An isolated settings file with every relevant env override cleared."""
    for name in (
        config_module.ENV_CONFIG_PATH,
        config_module.ENV_ALLOW_CLIPBOARD,
        config_module.ENV_ALLOW_KEYBOARD,
        config_module.ENV_SETTLE_QUIET_MS,
        config_module.ENV_SETTLE_POLL_MS,
        config_module.ENV_SETTLE_TIMEOUT_S,
        config_module.ENV_POST_ACTION_ENUM,
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "config.toml"


# ---------------------------------------------------------------------------
# defaults and file/env precedence
# ---------------------------------------------------------------------------


def test_defaults_are_the_planned_values(cfg):
    settings = config_module.load(cfg)
    assert settings.settle_quiet_ms == 40
    assert settings.settle_poll_ms == 10
    assert settings.settle_timeout_s == 3.0
    assert settings.post_action_enum is True
    assert settings.problems == ()


def test_the_file_sets_all_four_keys(cfg):
    cfg.write_text(
        "settle_quiet_ms = 200\n"
        "settle_poll_ms = 25\n"
        "settle_timeout_s = 10.0\n"
        "post_action_enum = false\n",
        encoding="utf-8",
    )
    settings = config_module.load(cfg)
    assert settings.settle_quiet_ms == 200
    assert settings.settle_poll_ms == 25
    assert settings.settle_timeout_s == 10.0
    assert settings.post_action_enum is False


def test_env_beats_the_file(cfg, monkeypatch):
    cfg.write_text(
        "settle_quiet_ms = 200\nsettle_poll_ms = 50\n"
        "settle_timeout_s = 10.0\npost_action_enum = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(config_module.ENV_SETTLE_QUIET_MS, "20")
    monkeypatch.setenv(config_module.ENV_SETTLE_POLL_MS, "5")
    monkeypatch.setenv(config_module.ENV_SETTLE_TIMEOUT_S, "1.5")
    monkeypatch.setenv(config_module.ENV_POST_ACTION_ENUM, "1")
    settings = config_module.load(cfg)
    assert settings.settle_quiet_ms == 20
    assert settings.settle_poll_ms == 5
    assert settings.settle_timeout_s == 1.5
    assert settings.post_action_enum is True


def test_a_bad_file_value_is_reported_and_keeps_the_default(cfg):
    """``"40"`` in the file is a typo, not a number: report it, do not coerce."""
    cfg.write_text(
        'settle_quiet_ms = "40"\nsettle_poll_ms = 500\n', encoding="utf-8"
    )
    settings = config_module.load(cfg)
    assert settings.settle_quiet_ms == 40
    assert settings.settle_poll_ms == 10
    assert len(settings.problems) == 2, settings.problems


def test_a_bad_env_value_is_reported_and_keeps_the_file_value(cfg, monkeypatch):
    cfg.write_text("settle_quiet_ms = 200\n", encoding="utf-8")
    monkeypatch.setenv(config_module.ENV_SETTLE_QUIET_MS, "40ms")
    settings = config_module.load(cfg)
    assert settings.settle_quiet_ms == 200
    assert any("SOMNUS_SETTLE_QUIET_MS" in p for p in settings.problems)


def test_the_four_keys_round_trip_through_save_and_load(cfg):
    config_module.save(
        config_module.Settings(
            settle_quiet_ms=80,
            settle_poll_ms=5,
            settle_timeout_s=2.5,
            post_action_enum=False,
        ),
        cfg,
    )
    text = cfg.read_text(encoding="utf-8")
    for key in ("settle_quiet_ms", "settle_poll_ms", "settle_timeout_s", "post_action_enum"):
        assert f"{key} = " in text, key

    settings = config_module.load(cfg)
    assert settings.settle_quiet_ms == 80
    assert settings.settle_poll_ms == 5
    assert settings.settle_timeout_s == 2.5
    assert settings.post_action_enum is False
    assert settings.problems == ()


def test_render_names_the_four_keys(cfg):
    text = config_module.load(cfg).render()
    for key in ("settle_quiet_ms", "settle_poll_ms", "settle_timeout_s", "post_action_enum"):
        assert key in text, key


# ---------------------------------------------------------------------------
# the configure tool
# ---------------------------------------------------------------------------


class StubSession:
    """A stand-in for the session-side ``apply_settings`` (a separate change).

    It implements the fixed signature and the "startup values" a reset returns
    to. Everything else the tool touches (``settler``, ``post_action_enum``) is
    present so the reply can be read back from the live object.
    """

    def __init__(self, quiet=40, poll=10, timeout=3.0, enum=True):
        self.settler = SimpleNamespace(
            quiet_ms=quiet, poll_interval_ms=poll, timeout_s=timeout
        )
        self.post_action_enum = enum
        self.startup = (quiet, poll, timeout, enum)
        self.calls: list[dict] = []

    def apply_settings(
        self,
        *,
        quiet_ms=None,
        poll_ms=None,
        timeout_s=None,
        post_action_enum=None,
        persist=False,
        reset=False,
    ):
        self.calls.append(
            {
                "quiet_ms": quiet_ms,
                "poll_ms": poll_ms,
                "timeout_s": timeout_s,
                "post_action_enum": post_action_enum,
                "persist": persist,
                "reset": reset,
            }
        )
        effective = (
            {
                "settle_quiet_ms": self.startup[0],
                "settle_poll_ms": self.startup[1],
                "settle_timeout_s": self.startup[2],
                "post_action_enum": self.startup[3],
            }
            if reset
            else {
                "settle_quiet_ms": self.settler.quiet_ms,
                "settle_poll_ms": self.settler.poll_interval_ms,
                "settle_timeout_s": self.settler.timeout_s,
                "post_action_enum": self.post_action_enum,
            }
        )
        if quiet_ms is not None:
            effective["settle_quiet_ms"] = quiet_ms
        if poll_ms is not None:
            effective["settle_poll_ms"] = poll_ms
        if timeout_s is not None:
            effective["settle_timeout_s"] = timeout_s
        if post_action_enum is not None:
            effective["post_action_enum"] = post_action_enum

        persisted = None
        if persist:
            persisted = config_module.persist_session_settings(**effective)

        self.settler.quiet_ms = effective["settle_quiet_ms"]
        self.settler.poll_interval_ms = effective["settle_poll_ms"]
        self.settler.timeout_s = effective["settle_timeout_s"]
        self.post_action_enum = effective["post_action_enum"]

        out = dict(effective)
        out["scope"] = "session"
        if persisted is not None:
            out["persisted"] = str(persisted)
        return out


def test_configure_echoes_the_effective_values():
    from somnus.protocol.tools import call_tool

    session = StubSession()
    out = call_tool(session, "configure", {})
    assert out == (
        "SETTINGS quiet_ms=40 poll_ms=10 timeout_s=3.0 "
        "post_action_enum=on scope=session"
    )
    assert session.calls and session.calls[0]["reset"] is False


def test_configure_applies_a_patch():
    from somnus.protocol.tools import call_tool

    session = StubSession()
    out = call_tool(
        session,
        "configure",
        {"settle_quiet_ms": 90, "post_action_enum": False},
    )
    assert "quiet_ms=90" in out
    assert "post_action_enum=off" in out
    assert session.settler.quiet_ms == 90
    assert session.post_action_enum is False


def test_one_bad_value_changes_nothing():
    from somnus.protocol.tools import call_tool

    session = StubSession()
    out = call_tool(
        session,
        "configure",
        {"settle_quiet_ms": 20, "settle_poll_ms": 999},
    )
    assert out.startswith("ERR INVALID_ARGUMENT")
    assert "settle_poll_ms" in out
    assert "nothing changed" in out
    assert session.calls == []
    assert session.settler.quiet_ms == 40


@pytest.mark.parametrize("bad", ["40", 40.5, True, -5, 1001])
def test_quiet_ms_is_strictly_an_integer_in_range(bad):
    from somnus.protocol.tools import call_tool

    out = call_tool(StubSession(), "configure", {"settle_quiet_ms": bad})
    assert out.startswith("ERR INVALID_ARGUMENT"), out
    assert "settle_quiet_ms" in out


def test_reset_returns_to_the_startup_values():
    from somnus.protocol.tools import call_tool

    session = StubSession()
    call_tool(session, "configure", {"settle_quiet_ms": 500})
    out = call_tool(session, "configure", {"reset": True})
    assert "quiet_ms=40" in out
    assert session.settler.quiet_ms == 40


def test_persist_writes_the_same_file_load_reads(cfg, monkeypatch):
    from somnus.protocol.tools import call_tool

    monkeypatch.setenv(config_module.ENV_CONFIG_PATH, str(cfg))
    session = StubSession()
    out = call_tool(
        session, "configure", {"settle_quiet_ms": 80, "persist": True}
    )
    assert out.startswith("SETTINGS quiet_ms=80")
    assert "persisted=" in out
    assert "session" in out

    settings = config_module.load(cfg)
    assert settings.settle_quiet_ms == 80
    # The untouched keys keep their previous/default values.
    assert settings.settle_poll_ms == 10
    assert settings.settle_timeout_s == 3.0
    assert settings.post_action_enum is True


def test_without_a_session_interface_the_tool_refuses_loudly():
    from somnus.protocol.tools import call_tool

    out = call_tool(object(), "configure", {"settle_quiet_ms": 40})
    assert out.startswith("ERR ACTION_NOT_SUPPORTED"), out


def test_the_tool_is_advertised_once():
    from somnus.protocol.tools import TOOL_SPECS

    names = [spec["name"] for spec in TOOL_SPECS]
    assert names.count("configure") == 1
    spec = next(s for s in TOOL_SPECS if s["name"] == "configure")
    assert set(spec["inputSchema"]["properties"]) == {
        "settle_quiet_ms",
        "settle_poll_ms",
        "settle_timeout_s",
        "post_action_enum",
        "persist",
        "reset",
    }


# ---------------------------------------------------------------------------
# find's optional timeout
# ---------------------------------------------------------------------------


class FindStub:
    """Records how ``find`` was called; the sentinel notices an omitted kwarg."""

    def __init__(self):
        self.calls: list = []

    def find(self, action, timeout_s="absent"):
        self.calls.append(timeout_s)
        return "OK"


def test_find_schema_advertises_timeout():
    from somnus.protocol.tools import TOOL_SPECS

    spec = next(s for s in TOOL_SPECS if s["name"] == "find")
    assert "timeout" in spec["inputSchema"]["properties"]


def test_find_timeout_is_passed_as_timeout_s():
    from somnus.protocol.tools import call_tool

    session = FindStub()
    out = call_tool(session, "find", {"query": "Big Mac", "timeout": 2})
    assert out == "OK"
    assert session.calls == [2.0]


def test_find_without_timeout_does_not_require_the_new_signature():
    from somnus.protocol.tools import call_tool

    session = FindStub()
    out = call_tool(session, "find", {"query": "Big Mac"})
    assert out == "OK"
    assert session.calls == ["absent"]


def test_find_timeout_zero_is_accepted():
    from somnus.protocol.tools import call_tool

    session = FindStub()
    assert call_tool(session, "find", {"query": "x", "timeout": 0}) == "OK"
    assert session.calls == [0.0]


@pytest.mark.parametrize(
    "bad", [-1, -0.5, "-1", "2", True, False, [2], float("nan"), float("inf")]
)
def test_find_timeout_rejects_invalid_values(bad):
    from somnus.protocol.tools import call_tool

    session = FindStub()
    out = call_tool(session, "find", {"query": "x", "timeout": bad})
    assert out.startswith("ERR INVALID_ARGUMENT"), out
    assert "timeout expected number >= 0" in out
    assert session.calls == []


class OldFindStub:
    """A session whose ``find`` predates the optional ``timeout_s``."""

    def find(self, action, *, limit=50):
        return "OK"


def test_find_with_no_timeout_still_works_on_an_old_signature():
    from somnus.protocol.tools import call_tool

    assert call_tool(OldFindStub(), "find", {"query": "x"}) == "OK"


def test_find_timeout_on_an_old_signature_is_reported_not_crashed():
    from somnus.protocol.tools import call_tool

    out = call_tool(OldFindStub(), "find", {"query": "x", "timeout": 1})
    assert out.startswith("ERR ACTION_NOT_SUPPORTED"), out


# ---------------------------------------------------------------------------
# the loading hint and the two hosts
# ---------------------------------------------------------------------------


def test_settings_brief_states_the_values_and_the_trade_offs():
    from somnus.protocol.mcp_server import settings_brief

    session = StubSession(quiet=120, poll=20, timeout=5.0, enum=False)
    text = settings_brief(session)
    assert "settle_quiet_ms=120" in text
    assert "settle_poll_ms=20" in text
    assert "settle_timeout_s=5.0" in text
    assert "post_action_enum=off" in text
    assert "smaller = faster" in text
    assert "can read an animation" in text
    assert "can hide it" in text or "hide" in text


def test_mcp_instructions_carry_the_brief():
    """The text the client actually receives at initialize, not just an attribute."""
    pytest.importorskip("mcp")
    from somnus.protocol.mcp_server import make_mcp_server, settings_brief

    session = StubSession()
    server = make_mcp_server(session)
    text = server.create_initialization_options().instructions
    assert text == settings_brief(session)
    for needle in (
        "settle_quiet_ms=40",
        "settle_poll_ms=10",
        "settle_timeout_s=3.0",
        "post_action_enum=on",
        "smaller = faster",
        "can read an animation",
        "can hide it",
        "configure",
    ):
        assert needle in text, needle

    # It is generated from the live values, not a constant: a different session
    # produces different text.
    other = make_mcp_server(StubSession(quiet=120, enum=False))
    other_text = other.create_initialization_options().instructions
    assert "settle_quiet_ms=120" in other_text
    assert "post_action_enum=off" in other_text
    assert other_text != text


def test_mcp_build_session_applies_the_file(cfg):
    from somnus.protocol.mcp_server import build_session

    config_module.save(
        config_module.Settings(
            settle_quiet_ms=150, settle_poll_ms=30,
            settle_timeout_s=6.0, post_action_enum=False,
        ),
        cfg,
    )
    session = build_session(provider="mock", config=str(cfg))
    try:
        assert session.settler.quiet_ms == 150
        assert session.settler.poll_interval_ms == 30
        assert session.settler.timeout_s == 6.0
        assert session.post_action_enum is False
    finally:
        session.close()


def test_cli_build_session_applies_the_file(cfg, monkeypatch):
    from somnus.cli import build_session

    config_module.save(
        config_module.Settings(
            settle_quiet_ms=90, settle_poll_ms=15,
            settle_timeout_s=2.0, post_action_enum=False,
        ),
        cfg,
    )
    monkeypatch.setenv(config_module.ENV_CONFIG_PATH, str(cfg))

    session = build_session(_args())
    try:
        assert session.settler.quiet_ms == 90
        assert session.settler.poll_interval_ms == 15
        assert session.settler.timeout_s == 2.0
        assert session.post_action_enum is False
    finally:
        session.close()


def test_persist_session_settings_writes_the_file(cfg, monkeypatch):
    monkeypatch.setenv(config_module.ENV_CONFIG_PATH, str(cfg))
    written = config_module.persist_session_settings(
        settle_quiet_ms=120,
        settle_poll_ms=20,
        settle_timeout_s=4.0,
        post_action_enum=False,
    )
    assert written == cfg
    settings = config_module.load(cfg)
    assert settings.settle_quiet_ms == 120
    assert settings.settle_poll_ms == 20
    assert settings.settle_timeout_s == 4.0
    assert settings.post_action_enum is False


def test_configure_works_against_the_real_session(cfg, monkeypatch):
    """One dispatch path, exercised with the session-side implementation."""
    from somnus.cli import build_session
    from somnus.protocol.tools import call_tool

    monkeypatch.setenv(config_module.ENV_CONFIG_PATH, str(cfg))
    session = build_session(_args())
    try:
        out = call_tool(session, "configure", {"settle_quiet_ms": 70})
        assert out.startswith("SETTINGS quiet_ms=70"), out
        assert session.settler.quiet_ms == 70

        out = call_tool(session, "configure", {"reset": True})
        assert "quiet_ms=40" in out
        assert session.settler.quiet_ms == 40

        out = call_tool(
            session, "configure", {"settle_quiet_ms": 80, "persist": True}
        )
        assert "persisted=" in out
        assert config_module.load(cfg).settle_quiet_ms == 80
    finally:
        session.close()


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        provider="mock",
        fixture=None,
        demo=True,
        pack=None,
        allow=None,
        allow_keyboard=None,
        allow_clipboard=None,
        config=None,
        no_pack=False,
        scenario=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)
