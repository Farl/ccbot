"""Tests for Slack directory browser session picker."""

from unittest.mock import MagicMock


def _make_session(
    session_id: str, cwd: str = "/tmp/proj", file_path: str = "/tmp/s.jsonl"
):
    from ccbot.session import ClaudeSession

    s = MagicMock(spec=ClaudeSession)
    s.session_id = session_id
    s.cwd = cwd
    s.file_path = file_path
    return s


def test_build_session_picker_returns_blocks_and_text():
    from ccbot.transports.slack.handlers.directory_browser import build_session_picker

    sessions = [_make_session("abc12345"), _make_session("xyz98765")]
    result = build_session_picker("U1", sessions, msg_ts="T1", cwd="/proj")
    assert "text" in result
    assert "blocks" in result
    assert len(result["blocks"]) > 0


def test_build_session_picker_stores_state():
    from ccbot.transports.slack.handlers.directory_browser import (
        build_session_picker,
        get_session_picker_state,
    )

    sessions = [_make_session("abc12345")]
    build_session_picker("U1", sessions, msg_ts="T1", cwd="/proj")
    state = get_session_picker_state("U1", msg_ts="T1")
    assert state is not None
    assert state["sessions"] == sessions


def test_clear_session_picker_state():
    from ccbot.transports.slack.handlers.directory_browser import (
        build_session_picker,
        clear_session_picker_state,
        get_session_picker_state,
    )

    sessions = [_make_session("abc12345")]
    build_session_picker("U1", sessions, msg_ts="T1")
    clear_session_picker_state("U1", msg_ts="T1")
    assert get_session_picker_state("U1", msg_ts="T1") is None


def test_build_session_picker_stores_pending_text():
    from ccbot.transports.slack.handlers.directory_browser import (
        build_session_picker,
        get_session_picker_state,
    )

    sessions = [_make_session("abc12345")]
    build_session_picker(
        "U1", sessions, msg_ts="T1", pending_text="hello", thread_ts="TH1"
    )
    state = get_session_picker_state("U1", msg_ts="T1")
    assert state["pending_text"] == "hello"
    assert state["thread_ts"] == "TH1"
