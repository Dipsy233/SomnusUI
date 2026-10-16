"""持久化设置：默认关闭，用户可以一次打开并一直生效。

前提：`--allow-clipboard` / `--allow-keyboard` 这类"会抢占焦点"的通道默认必须关闭，
但"每次调用都要带 flag"不是一个可用的 opt-in —— agent 根本控制不了自己是怎么被
启动的。所以开关要能写进一个**人可手改、LLM 也可改**的小文件。

本文件钉住三件事：

1. **默认安全**：文件不存在、读不动、内容损坏，一律回到"两个都关"；
2. **优先级明确**：命令行 flag > 环境变量 > 文件 > 内置默认；
3. **不静默**：坏配置降级时必须报告问题，而不是假装没这回事。
"""

from __future__ import annotations

import argparse

import pytest

from somnus import config as config_module
from somnus.config import Settings


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """An isolated settings file, with the env overrides cleared.

    ``load()`` consults ``SOMNUS_CONFIG`` and the two switches, so a developer's
    own environment must not leak into these tests.
    """
    for name in (
        config_module.ENV_CONFIG_PATH,
        config_module.ENV_ALLOW_CLIPBOARD,
        config_module.ENV_ALLOW_KEYBOARD,
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "config.toml"


# ---------------------------------------------------------------------------
# 默认：两个都关
# ---------------------------------------------------------------------------


def test_defaults_are_both_off(cfg):
    """文件不存在时，两个合成通道都必须关闭。

    这是整个项目的立身之本：agent 不假装有人在敲键盘。默认必须是关的，
    而不是"没配置所以随便"。
    """
    assert not cfg.exists()
    settings = config_module.load(cfg)
    assert settings.allow_clipboard is False
    assert settings.allow_keyboard is False
    assert settings.path == cfg
    assert settings.problems == ()


def test_a_broken_file_degrades_to_off_and_says_so(cfg):
    """损坏的配置必须降级为"全关"，且把问题报告出来。

    失败方向很重要：一个读不懂的配置文件，绝不能意外变成"全部允许"。
    降级的同时必须**说明**，否则用户以为自己打开了开关而实际没有。
    """
    cfg.write_text("this is not = = toml", encoding="utf-8")
    settings = config_module.load(cfg)
    assert settings.allow_clipboard is False
    assert settings.allow_keyboard is False
    assert settings.problems, "a malformed file must be reported"
    assert "cannot read" in settings.problems[0]


def test_a_wrong_value_type_is_reported_not_coerced(cfg):
    """类型不对时报告，不做真值转换。

    `allow_clipboard = "false"` 这种写法如果被当成非空字符串而视为 True，
    就是静默打开了抢焦点的通道——所以宁可报告也不猜。
    """
    cfg.write_text('allow_clipboard = "yes"\nallow_keyboard = 1\n', encoding="utf-8")
    settings = config_module.load(cfg)
    assert settings.allow_clipboard is False
    assert settings.allow_keyboard is False
    assert len(settings.problems) == 2, settings.problems


def test_unknown_keys_are_ignored_with_a_note(cfg):
    """未知键忽略但提示：手改的文件里写点注释/别的键不该让加载失败。"""
    cfg.write_text("allow_clipboard = true\nfuture_option = 5\n", encoding="utf-8")
    settings = config_module.load(cfg)
    assert settings.allow_clipboard is True
    assert any("future_option" in p for p in settings.problems)


# ---------------------------------------------------------------------------
# 读写往返
# ---------------------------------------------------------------------------


def test_save_then_load_round_trips(cfg):
    written = config_module.save(
        Settings(allow_clipboard=True, allow_keyboard=False), cfg
    )
    assert written == cfg
    settings = config_module.load(cfg)
    assert settings.allow_clipboard is True
    assert settings.allow_keyboard is False


def test_the_written_file_is_hand_editable_and_self_explanatory(cfg):
    """写出来的文件必须能被人直接看懂并修改。

    它同时是给 LLM 编辑的：没有注释的裸键值对，模型改起来只能靠猜，
    用户打开也不知道这两个开关会让窗口弹到前台。
    """
    config_module.save(Settings(), cfg)
    text = cfg.read_text(encoding="utf-8")
    assert "allow_clipboard = false" in text
    assert "allow_keyboard = false" in text
    # 必须解释代价：这两个开关会抢占前台。
    assert "foreground" in text
    assert "#" in text, "comments are how a person knows what these do"


def test_saving_preserves_comments_instead_of_rewriting_bare_keys(cfg):
    """保存不能把注释洗掉——否则每保存一次，文件就更难改一次。"""
    config_module.save(Settings(allow_clipboard=True), cfg)
    text = cfg.read_text(encoding="utf-8")
    assert "Ctrl+V channel" in text
    assert "SendInput channel" in text


def test_allow_processes_round_trips(cfg):
    config_module.save(
        Settings(allow_processes=("notepad.exe", "qq.exe")), cfg
    )
    settings = config_module.load(cfg)
    assert settings.allow_processes == ("notepad.exe", "qq.exe")


def test_a_bad_allow_processes_value_is_reported(cfg):
    cfg.write_text('allow_processes = "notepad.exe"\n', encoding="utf-8")
    settings = config_module.load(cfg)
    assert settings.allow_processes == ()
    assert any("allow_processes" in p for p in settings.problems)


# ---------------------------------------------------------------------------
# 优先级：flag > env > file > default
# ---------------------------------------------------------------------------


def test_the_file_enables_a_channel(cfg):
    """文件写 true，就真的打开——这是"持久化 opt-in"的最小承诺。"""
    cfg.write_text("allow_clipboard = true\n", encoding="utf-8")
    assert config_module.load(cfg).allow_clipboard is True


def test_env_can_turn_a_file_setting_off(cfg, monkeypatch):
    """环境变量能反向覆盖：文件开着，一次运行可以关掉。

    只支持"打开"是不够的——CI 或临时调试需要一个不改文件的关闸。
    """
    cfg.write_text("allow_clipboard = true\n", encoding="utf-8")
    monkeypatch.setenv(config_module.ENV_ALLOW_CLIPBOARD, "0")
    assert config_module.load(cfg).allow_clipboard is False


def test_env_can_turn_a_channel_on_without_a_file(cfg, monkeypatch):
    monkeypatch.setenv(config_module.ENV_ALLOW_KEYBOARD, "1")
    settings = config_module.load(cfg)
    assert settings.allow_keyboard is True
    assert settings.allow_clipboard is False


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", ""])
def test_falsey_env_values_are_all_recognised(cfg, monkeypatch, value):
    """常见的"假"写法都要认，否则一个 `false` 会被当成打开。"""
    cfg.write_text("allow_clipboard = true\n", encoding="utf-8")
    monkeypatch.setenv(config_module.ENV_ALLOW_CLIPBOARD, value)
    assert config_module.load(cfg).allow_clipboard is False


def test_the_env_config_path_selects_the_file(tmp_path, monkeypatch):
    """SOMNUS_CONFIG 指定文件位置（测试与打包用）。"""
    target = tmp_path / "elsewhere" / "custom.toml"
    target.parent.mkdir(parents=True)
    target.write_text("allow_keyboard = true\n", encoding="utf-8")
    monkeypatch.setenv(config_module.ENV_CONFIG_PATH, str(target))
    settings = config_module.load()
    assert settings.path == target
    assert settings.allow_keyboard is True


def test_a_flag_overrides_the_file_end_to_end(cfg, monkeypatch):
    """命令行 flag 仍然优先于文件：一次性的覆盖不该要求先改文件。

    验证方式是走真实的 build_session，因为优先级是在那里被应用的。
    """
    from somnus.cli import build_session

    config_module.save(Settings(allow_clipboard=False), cfg)
    monkeypatch.setenv(config_module.ENV_CONFIG_PATH, str(cfg))

    session = build_session(_args(allow_clipboard=True))
    assert session.policy.allow_clipboard is True

    session = build_session(_args(allow_clipboard=False))
    assert session.policy.allow_clipboard is False


def test_build_session_reads_the_file(cfg, monkeypatch):
    """不传 flag 时，会话策略必须来自文件——这就是持久化的意义。"""
    from somnus.cli import build_session

    config_module.save(Settings(allow_clipboard=True, allow_keyboard=False), cfg)
    monkeypatch.setenv(config_module.ENV_CONFIG_PATH, str(cfg))

    session = build_session(_args())
    assert session.policy.allow_clipboard is True
    assert session.policy.allow_keyboard is False


def test_cli_config_command_toggles_and_reports(cfg, monkeypatch, capsys):
    """`somnus config --allow-clipboard` 写文件并回显当前状态。"""
    from somnus.cli import cmd_config

    args = argparse.Namespace(
        config=str(cfg), path=False, set_clipboard=True, set_keyboard=None
    )
    assert cmd_config(args) == 0
    out = capsys.readouterr().out
    assert "allow_clipboard  : True" in out
    assert config_module.load(cfg).allow_clipboard is True


def test_cli_config_command_writes_defaults_on_first_use(cfg, capsys):
    """首次运行 `config` 时把默认文件写出来，而不是只说一句"没有文件"。

    指向一个不存在的路径是死胡同；用户需要一个可以直接打开编辑的文件。
    """
    from somnus.cli import cmd_config

    args = argparse.Namespace(
        config=str(cfg), path=False, set_clipboard=None, set_keyboard=None
    )
    assert cmd_config(args) == 0
    assert cfg.exists(), "first run must create an editable file"
    assert "both channels off" in capsys.readouterr().out


def _args(**overrides) -> argparse.Namespace:
    """A minimal Namespace for build_session (mock provider, no fixtures)."""
    base = dict(
        provider="mock",
        fixture=None,
        demo=True,
        pack=None,
        allow=None,
        confirmed=False,
        allow_keyboard=False,
        allow_clipboard=False,
        config=None,
        no_pack=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)
