"""Per-user message queue for ordered Slack message delivery.

Provides FIFO queue processing with:
  - Message merging (consecutive content messages up to 3800 chars)
  - tool_use/tool_result pairing (tool_result edits tool_use message in-place)
  - Status-to-content conversion (first content edits existing status message)
  - Content-type formatting (build_response_parts)

Fork of transports/telegram/handlers/message_queue.py adapted for Slack:
  - user_id is str (not int)
  - thread_ts is str (not int thread_id)
  - channel stored in MessageTask (resolved at enqueue time)
  - Status coordination via take_status_ts/register_status_ts (no direct dict access)

Key components:
  - MessageTask: queued message task
  - build_response_parts: format text by content type (pure, no API calls)
  - get_or_create_queue: get/create queue + worker per user
  - enqueue_content_message: enqueue a content message
  - clear_tool_msg_ids: clean up on thread close
  - shutdown_workers: cancel all workers on shutdown
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal

from slack_sdk.web.async_client import AsyncWebClient

from ....terminal_parser import parse_status_line
from ....tmux_manager import tmux_manager
from ..splitter import split_message
from .message_sender import delete_message, edit_message, send_message
from .status_polling import register_status_ts, take_status_ts

logger = logging.getLogger(__name__)

MERGE_MAX_LENGTH = 3800
_MAX_PART_LENGTH = 3000  # headroom for mrkdwn expansion at send layer


# ---------------------------------------------------------------------------
# Content-type formatting
# ---------------------------------------------------------------------------


def build_response_parts(
    text: str,
    is_complete: bool,
    content_type: str = "text",
    role: str = "assistant",
) -> list[str]:
    """Format a message into Slack-ready string parts (no API calls).

    Markdown-to-mrkdwn conversion happens at the send layer, not here.
    Multi-part messages get a [1/N] suffix.
    """
    text = text.strip()

    if role == "user":
        if len(text) > 3000:
            text = text[:3000] + "…"
        return [f"👤 {text}"]

    if content_type == "thinking":
        max_thinking = 500
        if len(text) > max_thinking:
            text = text[:max_thinking] + "\n\n… (thinking truncated)"
        quoted = "\n".join(f"> {line}" for line in text.splitlines())
        return [f"> _Thinking…_\n{quoted}"]

    chunks = split_message(text, max_length=_MAX_PART_LENGTH)
    total = len(chunks)
    if total == 1:
        return [chunks[0]]
    return [f"{chunk}\n\n[{i}/{total}]" for i, chunk in enumerate(chunks, 1)]


# ---------------------------------------------------------------------------
# Queue data model
# ---------------------------------------------------------------------------


@dataclass
class MessageTask:
    """Message task for Slack queue processing."""

    task_type: Literal["content"]
    channel: str = ""
    thread_ts: str = ""
    window_id: str = ""
    parts: list[str] = field(default_factory=list)
    tool_use_id: str | None = None
    content_type: str = "text"
    text: str | None = None


# Per-user queues; user_id is str for Slack
_message_queues: dict[str, asyncio.Queue[MessageTask]] = {}
_queue_workers: dict[str, asyncio.Task[None]] = {}
_queue_locks: dict[str, asyncio.Lock] = {}

# (tool_use_id, user_id, thread_ts) -> Slack message ts
_tool_msg_ids: dict[tuple[str, str, str], str] = {}


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------


def _can_merge_tasks(base: MessageTask, candidate: MessageTask) -> bool:
    """Return True if candidate can be merged into base."""
    if base.window_id != candidate.window_id:
        return False
    if candidate.task_type != "content":
        return False
    if base.content_type in ("tool_use", "tool_result"):
        return False
    if candidate.content_type in ("tool_use", "tool_result"):
        return False
    return True


def _inspect_queue(queue: asyncio.Queue[MessageTask]) -> list[MessageTask]:
    """Drain queue non-destructively; caller must refill."""
    items: list[MessageTask] = []
    while not queue.empty():
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return items


async def _merge_content_tasks(
    queue: asyncio.Queue[MessageTask],
    first: MessageTask,
    lock: asyncio.Lock,
) -> tuple[MessageTask, int]:
    """Merge consecutive mergeable tasks into first.

    Items put back are marked task_done() to compensate for put_nowait()'s
    internal counter increment (items were already counted when enqueued).
    Returns (merged_task, merge_count).
    """
    merged_parts = list(first.parts)
    current_length = sum(len(p) for p in merged_parts)
    merge_count = 0

    async with lock:
        items = _inspect_queue(queue)
        remaining: list[MessageTask] = []

        for i, task in enumerate(items):
            if not _can_merge_tasks(first, task):
                remaining = items[i:]
                break
            task_length = sum(len(p) for p in task.parts)
            if current_length + task_length > MERGE_MAX_LENGTH:
                remaining = items[i:]
                break
            merged_parts.extend(task.parts)
            current_length += task_length
            merge_count += 1

        for item in remaining:
            queue.put_nowait(item)
            queue.task_done()

    if merge_count == 0:
        return first, 0
    return (
        MessageTask(
            task_type="content",
            channel=first.channel,
            thread_ts=first.thread_ts,
            window_id=first.window_id,
            parts=merged_parts,
            tool_use_id=first.tool_use_id,
            content_type=first.content_type,
        ),
        merge_count,
    )


# ---------------------------------------------------------------------------
# Task processing
# ---------------------------------------------------------------------------


async def _process_content_task(
    client: AsyncWebClient,
    user_id: str,
    task: MessageTask,
) -> None:
    """Process one content task: send or edit message."""
    channel = task.channel
    thread_ts = task.thread_ts
    wid = task.window_id

    # 1. tool_result: edit the matching tool_use message in-place
    if task.content_type == "tool_result" and task.tool_use_id:
        tkey = (task.tool_use_id, user_id, thread_ts)
        edit_ts = _tool_msg_ids.pop(tkey, None)
        if edit_ts:
            full_text = "\n\n".join(task.parts)
            ok = await edit_message(client, channel, edit_ts, full_text)
            if ok:
                await _check_and_send_status(client, user_id, wid, channel, thread_ts)
                return
            # edit failed — fall through to send new message

    # 2. Send content; convert status message to content on first part
    first_part = True
    last_ts: str | None = None
    for part in task.parts:
        if first_part:
            first_part = False
            converted_ts = await _convert_status_to_content(
                client, user_id, thread_ts, wid, channel, part
            )
            if converted_ts:
                last_ts = converted_ts
                continue
        sent_ts = await send_message(client, channel, part, thread_ts=thread_ts)
        if sent_ts:
            last_ts = sent_ts

    # 3. Record tool_use ts for later in-place editing by tool_result
    if last_ts and task.tool_use_id and task.content_type == "tool_use":
        _tool_msg_ids[(task.tool_use_id, user_id, thread_ts)] = last_ts

    # 4. After content, re-check terminal for a status line
    await _check_and_send_status(client, user_id, wid, channel, thread_ts)


async def _convert_status_to_content(
    client: AsyncWebClient,
    user_id: str,
    thread_ts: str,
    window_id: str,
    channel: str,
    content_text: str,
) -> str | None:
    """Edit the existing status message to show content.

    Uses take_status_ts() to atomically claim the status message.
    Returns the message ts on success, None if nothing to convert.
    """
    status_ts = take_status_ts(user_id, thread_ts)
    if not status_ts:
        return None
    ok = await edit_message(client, channel, status_ts, content_text)
    if ok:
        return status_ts
    # Edit failed (message deleted/expired) — delete silently
    await delete_message(client, channel, status_ts)
    return None


async def _check_and_send_status(
    client: AsyncWebClient,
    user_id: str,
    window_id: str,
    channel: str,
    thread_ts: str,
) -> None:
    """After delivering content, check terminal for status and send if present."""
    queue = _message_queues.get(user_id)
    if queue and not queue.empty():
        return
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        return
    status_line = parse_status_line(pane_text)
    if status_line:
        ts = await send_message(client, channel, status_line, thread_ts=thread_ts)
        if ts:
            register_status_ts(user_id, thread_ts, ts, window_id, status_line)


# ---------------------------------------------------------------------------
# Queue worker and lifecycle
# ---------------------------------------------------------------------------


async def _queue_worker(client: AsyncWebClient, user_id: str) -> None:
    """Process message tasks for one user sequentially."""
    queue = _message_queues[user_id]
    lock = _queue_locks[user_id]
    logger.info("Slack message queue worker started for user %s", user_id)
    while True:
        try:
            task = await queue.get()
            try:
                merged_task, merge_count = await _merge_content_tasks(queue, task, lock)
                for _ in range(merge_count):
                    queue.task_done()
                await _process_content_task(client, user_id, merged_task)
            except Exception as e:
                logger.error("Error processing task for user %s: %s", user_id, e)
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            logger.info("Slack queue worker cancelled for user %s", user_id)
            break
        except Exception as e:
            logger.error("Unexpected queue worker error for user %s: %s", user_id, e)


def get_or_create_queue(
    client: AsyncWebClient, user_id: str
) -> asyncio.Queue[MessageTask]:
    """Get or create queue and worker for a user."""
    if user_id not in _message_queues:
        _message_queues[user_id] = asyncio.Queue()
        _queue_locks[user_id] = asyncio.Lock()
        _queue_workers[user_id] = asyncio.create_task(_queue_worker(client, user_id))
    return _message_queues[user_id]


async def enqueue_content_message(
    client: AsyncWebClient,
    user_id: str,
    channel: str,
    thread_ts: str,
    window_id: str,
    parts: list[str],
    tool_use_id: str | None = None,
    content_type: str = "text",
    text: str | None = None,
) -> None:
    """Enqueue a content message task for delivery."""
    queue = get_or_create_queue(client, user_id)
    queue.put_nowait(
        MessageTask(
            task_type="content",
            channel=channel,
            thread_ts=thread_ts,
            window_id=window_id,
            parts=parts,
            tool_use_id=tool_use_id,
            content_type=content_type,
            text=text,
        )
    )


def clear_tool_msg_ids(user_id: str, thread_ts: str) -> None:
    """Remove all tool message ID tracking for a user/thread (called on cleanup)."""
    keys = [k for k in _tool_msg_ids if k[1] == user_id and k[2] == thread_ts]
    for k in keys:
        _tool_msg_ids.pop(k, None)


async def shutdown_workers() -> None:
    """Cancel all queue workers (call on bot shutdown)."""
    for worker in list(_queue_workers.values()):
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    _queue_workers.clear()
    _message_queues.clear()
    _queue_locks.clear()
    logger.info("Slack message queue workers stopped")
