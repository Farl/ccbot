"""Tests for Slack history handler."""


def test_build_history_nav_blocks_single_page():
    from ccbot.transports.slack.handlers.history import build_history_nav_blocks

    assert build_history_nav_blocks(window_id="@0", page=0, total_pages=1) == []


def test_build_history_nav_blocks_prev_only():
    from ccbot.transports.slack.handlers.history import build_history_nav_blocks

    blocks = build_history_nav_blocks(window_id="@0", page=2, total_pages=3)
    action_ids = [
        e["action_id"]
        for b in blocks
        for e in b.get("elements", [])
        if "action_id" in e
    ]
    assert any("prev" in a for a in action_ids)
    assert not any("next" in a for a in action_ids)


def test_build_history_nav_blocks_both():
    from ccbot.transports.slack.handlers.history import build_history_nav_blocks

    blocks = build_history_nav_blocks(window_id="@0", page=1, total_pages=3)
    action_ids = [
        e["action_id"]
        for b in blocks
        for e in b.get("elements", [])
        if "action_id" in e
    ]
    assert any("prev" in a for a in action_ids)
    assert any("next" in a for a in action_ids)


def test_parse_history_action_id_prev():
    from ccbot.transports.slack.handlers.history import parse_history_action_id

    page, window_id = parse_history_action_id("hist_prev_2_@5")
    assert page == 1
    assert window_id == "@5"


def test_parse_history_action_id_next():
    from ccbot.transports.slack.handlers.history import parse_history_action_id

    page, window_id = parse_history_action_id("hist_next_2_@5")
    assert page == 3
    assert window_id == "@5"
