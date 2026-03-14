"""Tests for Slack command handler."""


def test_parse_text_command_esc():
    from ccbot.transports.slack.handlers.commands import parse_text_command

    assert parse_text_command("!esc") == ("esc", [])


def test_parse_text_command_history_with_page():
    from ccbot.transports.slack.handlers.commands import parse_text_command

    assert parse_text_command("!history 2") == ("history", ["2"])


def test_parse_text_command_not_a_command():
    from ccbot.transports.slack.handlers.commands import parse_text_command

    assert parse_text_command("hello world") is None


def test_parse_text_command_empty():
    from ccbot.transports.slack.handlers.commands import parse_text_command

    assert parse_text_command("") is None


def test_parse_text_command_exclamation_only():
    from ccbot.transports.slack.handlers.commands import parse_text_command

    assert parse_text_command("!") is None


def test_parse_text_command_unbind():
    from ccbot.transports.slack.handlers.commands import parse_text_command

    assert parse_text_command("!unbind") == ("unbind", [])
