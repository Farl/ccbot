"""Tests for the CLI entry point (main.main) — argv dispatch.

Regression guard for the upstream-sync bug where a Telegram-only argv guard
rejected our fork's `--transport` flag before argparse ever saw it, breaking
`ccbot --transport slack` (the exact command scripts/restart.sh uses).
"""

from unittest.mock import MagicMock

import pytest


def _patch_bootstrap(monkeypatch, *, slack=True, telegram=True):
    """Neutralize main()'s heavy bootstrap so only argv dispatch is exercised."""
    from ccbot import hook as hook_mod
    from ccbot.config import config
    from ccbot.tmux_manager import tmux_manager
    from ccbot.transports.slack import bot as slack_bot
    from ccbot.transports.telegram import bot as tg_bot

    monkeypatch.setattr(hook_mod, "hook_main", MagicMock(), raising=True)
    session = MagicMock()
    session.session_name = "ccbot"
    monkeypatch.setattr(
        tmux_manager, "get_or_create_session", MagicMock(return_value=session)
    )
    if slack:
        monkeypatch.setattr(config, "slack_bot_token", "xoxb-test")
        monkeypatch.setattr(config, "slack_app_token", "xapp-test")
    monkeypatch.setattr(slack_bot, "run_slack_bot", MagicMock())
    monkeypatch.setattr(tg_bot, "create_bot", MagicMock(return_value=MagicMock()))


def test_transport_slack_is_accepted_and_dispatched(monkeypatch):
    from ccbot import main as main_mod
    from ccbot.transports.slack import bot as slack_bot

    _patch_bootstrap(monkeypatch)
    monkeypatch.setattr("sys.argv", ["ccbot", "--transport", "slack"])
    main_mod.main()
    slack_bot.run_slack_bot.assert_called_once()


def test_transport_telegram_is_accepted_and_dispatched(monkeypatch):
    from ccbot import main as main_mod
    from ccbot.transports.telegram import bot as tg_bot

    _patch_bootstrap(monkeypatch)
    monkeypatch.setattr("sys.argv", ["ccbot", "--transport", "telegram"])
    main_mod.main()
    tg_bot.create_bot.assert_called_once()
    tg_bot.create_bot.return_value.run_polling.assert_called_once()


def test_hook_subcommand_dispatches_to_hook_main(monkeypatch):
    from ccbot import hook as hook_mod
    from ccbot import main as main_mod
    from ccbot.transports.slack import bot as slack_bot

    _patch_bootstrap(monkeypatch)
    monkeypatch.setattr("sys.argv", ["ccbot", "hook"])
    main_mod.main()
    hook_mod.hook_main.assert_called_once()
    slack_bot.run_slack_bot.assert_not_called()


def test_unknown_argument_exits_2(monkeypatch):
    """A typo must not silently fall through to launching a racing bot."""
    from ccbot import main as main_mod

    _patch_bootstrap(monkeypatch)
    monkeypatch.setattr("sys.argv", ["ccbot", "--transprot", "slack"])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 2
