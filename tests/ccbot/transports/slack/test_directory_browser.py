"""Tests for Slack directory browser session picker."""


def _make_session(
    session_id: str,
    file_path: str = "/tmp/s.jsonl",
    *,
    summary: str = "some work",
    is_sdk: bool = False,
    is_running: bool = False,
):
    from ccbot.session import ClaudeSession

    return ClaudeSession(
        session_id=session_id,
        summary=summary,
        message_count=5,
        file_path=file_path,
        is_sdk=is_sdk,
        is_running=is_running,
    )


def _button_labels(result: dict) -> list[str]:
    """Extract all button plain_text labels from picker blocks."""
    labels = []
    for b in result["blocks"]:
        for el in b.get("elements", []):
            labels.append(el["text"]["text"])
    return labels


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


def test_running_session_marked():
    from ccbot.transports.slack.handlers.directory_browser import build_session_picker

    sessions = [_make_session("run00001", is_running=True, summary="live one")]
    labels = _button_labels(build_session_picker("U1", sessions, msg_ts="T1"))
    assert any("執行中" in lbl and "live one" in lbl for lbl in labels)


def test_sdk_session_tagged_and_toggle_offered():
    from ccbot.transports.slack.handlers.directory_browser import build_session_picker

    sessions = [
        _make_session("inter001", summary="typed"),
        _make_session("sdk00001", is_sdk=True, summary="cron run"),
    ]
    labels = _button_labels(build_session_picker("U1", sessions, msg_ts="T1"))
    assert any("[-p]" in lbl for lbl in labels)  # sdk session tagged
    assert any("隱藏 -p" in lbl for lbl in labels)  # toggle offered


def test_hide_sdk_filters_and_reindexes():
    from ccbot.transports.slack.handlers.directory_browser import (
        build_session_picker,
        get_session_picker_state,
    )

    sessions = [
        _make_session("inter001", summary="typed"),
        _make_session("sdk00001", is_sdk=True, summary="cron"),
    ]
    build_session_picker("U1", sessions, msg_ts="T1", hide_sdk=True)
    state = get_session_picker_state("U1", msg_ts="T1")
    # Displayed list (SELECT index space) excludes the sdk session...
    assert [s.session_id for s in state["sessions"]] == ["inter001"]
    # ...but the full set is retained so the toggle can bring it back.
    assert len(state["all_sessions"]) == 2


def test_no_toggle_when_no_sdk_sessions():
    from ccbot.transports.slack.handlers.directory_browser import build_session_picker

    sessions = [_make_session("inter001"), _make_session("inter002")]
    labels = _button_labels(build_session_picker("U1", sessions, msg_ts="T1"))
    assert not any("-p" in lbl for lbl in labels)
