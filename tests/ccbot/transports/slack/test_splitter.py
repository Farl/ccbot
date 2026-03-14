"""Tests for Slack message splitter."""

from ccbot.transports.slack.splitter import SLACK_MAX_LENGTH, split_message


def test_short_message_single_part():
    """Short message returns single part."""
    result = split_message("hello world")
    assert result == ["hello world"]


def test_long_message_splits():
    """Long message splits into multiple parts."""
    text = "a" * 5000
    result = split_message(text, max_length=4000)
    assert len(result) == 2
    assert "".join(result) == text


def test_split_prefers_newline_boundaries():
    """Split prefers newline boundaries."""
    lines = ["line " + str(i) for i in range(500)]
    text = "\n".join(lines)
    result = split_message(text, max_length=4000)
    assert len(result) > 1
    for part in result:
        assert len(part) <= 4000
    assert "\n".join(result) == text


def test_single_long_line_hard_split():
    """Single line longer than max gets hard-split."""
    line = "x" * 10000
    result = split_message(line, max_length=4000)
    assert len(result) == 3
    assert "".join(result) == line
    for part in result:
        assert len(part) <= 4000


def test_all_parts_within_limit():
    """All parts are <= max_length."""
    text = ("a" * 3999 + "\n") * 5
    result = split_message(text, max_length=4000)
    for part in result:
        assert len(part) <= 4000


def test_default_max_length():
    """Default max_length is SLACK_MAX_LENGTH."""
    assert SLACK_MAX_LENGTH == 4000
    short = "hi"
    assert split_message(short) == [short]
