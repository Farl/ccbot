"""Tests for Slack message queue."""

import asyncio
import pytest


def _make_task(
    content_type="text",
    parts=None,
    tool_use_id=None,
    channel="C1",
    thread_ts="T1",
    window_id="@0",
):
    from ccbot.transports.slack.handlers.message_queue import MessageTask

    return MessageTask(
        task_type="content",
        content_type=content_type,
        parts=parts or ["hello"],
        tool_use_id=tool_use_id,
        channel=channel,
        thread_ts=thread_ts,
        window_id=window_id,
    )


def test_can_merge_same_window():
    from ccbot.transports.slack.handlers.message_queue import _can_merge_tasks

    a = _make_task(window_id="@0")
    b = _make_task(window_id="@0")
    assert _can_merge_tasks(a, b) is True


def test_cannot_merge_different_window():
    from ccbot.transports.slack.handlers.message_queue import _can_merge_tasks

    assert (
        _can_merge_tasks(_make_task(window_id="@0"), _make_task(window_id="@1"))
        is False
    )


def test_cannot_merge_tool_use_base():
    from ccbot.transports.slack.handlers.message_queue import _can_merge_tasks

    assert _can_merge_tasks(_make_task(content_type="tool_use"), _make_task()) is False


def test_cannot_merge_tool_use_candidate():
    from ccbot.transports.slack.handlers.message_queue import _can_merge_tasks

    assert _can_merge_tasks(_make_task(), _make_task(content_type="tool_use")) is False


def test_cannot_merge_tool_result():
    from ccbot.transports.slack.handlers.message_queue import _can_merge_tasks

    assert (
        _can_merge_tasks(_make_task(content_type="tool_result"), _make_task()) is False
    )


@pytest.mark.asyncio
async def test_merge_combines_parts():
    from ccbot.transports.slack.handlers.message_queue import (
        MessageTask,
        _merge_content_tasks,
    )

    queue: asyncio.Queue[MessageTask] = asyncio.Queue()
    lock = asyncio.Lock()
    first = _make_task(parts=["hello"])
    second = _make_task(parts=["world"])
    await queue.put(second)
    queue.task_done()  # compensate put_nowait counter
    merged, count = await _merge_content_tasks(queue, first, lock)
    assert count == 1
    assert merged.parts == ["hello", "world"]


@pytest.mark.asyncio
async def test_merge_stops_at_limit():
    from ccbot.transports.slack.handlers.message_queue import (
        MERGE_MAX_LENGTH,
        MessageTask,
        _merge_content_tasks,
    )

    queue: asyncio.Queue[MessageTask] = asyncio.Queue()
    lock = asyncio.Lock()
    big = "x" * MERGE_MAX_LENGTH
    first = _make_task(parts=[big])
    second = _make_task(parts=["overflow"])
    await queue.put(second)
    queue.task_done()
    merged, count = await _merge_content_tasks(queue, first, lock)
    assert count == 0
    assert merged.parts == [big]


def test_build_response_parts_text():
    from ccbot.transports.slack.handlers.message_queue import build_response_parts

    assert build_response_parts("hello", is_complete=True) == ["hello"]


def test_build_response_parts_user_echo():
    from ccbot.transports.slack.handlers.message_queue import build_response_parts

    parts = build_response_parts("question", is_complete=True, role="user")
    assert parts[0].startswith("👤 ")


def test_build_response_parts_thinking():
    from ccbot.transports.slack.handlers.message_queue import build_response_parts

    parts = build_response_parts("internal", is_complete=True, content_type="thinking")
    assert parts[0].startswith(">")


def test_build_response_parts_long_splits():
    from ccbot.transports.slack.handlers.message_queue import build_response_parts

    parts = build_response_parts("x" * 5000, is_complete=True)
    assert len(parts) > 1
    assert "[1/" in parts[0]
