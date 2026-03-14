"""Terminal status line polling for Slack transport.

Background task that polls terminal status lines for all thread-bound windows
at 1-second intervals. Sends or edits Slack messages to show current status.
Detects interactive UIs and delegates to the interactive UI handler.

Key state:
  _status_msgs: (user_id, thread_ts) -> (msg_ts, window_id, last_text)
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from slack_sdk.web.async_client import AsyncWebClient

from ....config import config
from ....session import session_manager
from ....terminal_parser import is_interactive_ui, parse_status_line
from ....tmux_manager import tmux_manager
from .interactive_ui import (
    CLEAR_GRACE_MISSES,
    _grace_counters,
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .message_sender import delete_message, edit_message, send_message

logger = logging.getLogger(__name__)

STATUS_POLL_INTERVAL = 1.0  # seconds

# Seconds to suppress new interactive UI after the last session monitor delivery.
# Gives the session monitor time to flush all pending tool/response messages first.
_CONTENT_SETTLE_TIME = 3.0

# (user_id, thread_ts) -> monotonic time of last content delivery
_last_content_time: dict[tuple[str, str], float] = {}

# (user_id, thread_ts) -> (msg_ts, window_id, last_text)
_status_msgs: dict[tuple[str, str], tuple[str, str, str]] = {}


def record_content_delivery(user_id: str, thread_ts: str) -> None:
    """Called by handle_new_message to signal content was just delivered."""
    _last_content_time[(user_id, thread_ts)] = time.monotonic()


def _is_content_settling(user_id: str, thread_ts: str) -> bool:
    """True if content was recently delivered — more messages may be in flight."""
    t = _last_content_time.get((user_id, thread_ts), 0.0)
    return (time.monotonic() - t) < _CONTENT_SETTLE_TIME


async def update_status_for_window(
    client: AsyncWebClient,
    user_id: str,
    thread_ts: str,
    window_id: str,
    channel: str,
) -> None:
    """Poll terminal and check for interactive UIs and status updates."""
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        await clear_status(user_id, thread_ts, client, channel)
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        return

    interactive_window = get_interactive_window(user_id, thread_ts)
    should_check_new_ui = True

    ikey = (user_id, thread_ts)

    if interactive_window == window_id:
        if is_interactive_ui(pane_text):
            _grace_counters.pop(ikey, None)  # Reset miss counter
            # Update display in case user navigated (content may have changed)
            await handle_interactive_ui(
                client, user_id, thread_ts, window_id, channel, pane_text
            )
            return
        # UI not detected — apply grace period before clearing
        miss = _grace_counters.get(ikey, 0) + 1
        _grace_counters[ikey] = miss
        if miss < CLEAR_GRACE_MISSES:
            return  # Not enough consecutive misses yet
        # Grace period exceeded — clear interactive mode
        _grace_counters.pop(ikey, None)
        await clear_interactive_msg(client, user_id, thread_ts, channel)
        should_check_new_ui = False
    elif interactive_window is not None:
        _grace_counters.pop(ikey, None)
        await clear_interactive_msg(client, user_id, thread_ts, channel)

    if should_check_new_ui and is_interactive_ui(pane_text):
        if _is_content_settling(user_id, thread_ts):
            # Content was just delivered — wait for session monitor to flush remaining
            # messages before posting the interactive UI on top of them.
            return
        logger.debug(
            "Interactive UI detected in polling (user=%s, window=%s)",
            user_id,
            window_id,
        )
        await handle_interactive_ui(
            client, user_id, thread_ts, window_id, channel, pane_text
        )
        return

    status_line = parse_status_line(pane_text)
    if not status_line:
        # Claude may have exited — clear any stale status message
        await clear_status(user_id, thread_ts, client, channel)
        return
    if not config.show_status or session_manager.is_silent(window_id):
        return

    skey = (user_id, thread_ts)
    existing = _status_msgs.get(skey)

    if existing:
        msg_ts, existing_wid, last_text = existing
        if existing_wid == window_id and last_text == status_line:
            return  # Dedup — identical content
        ok = await edit_message(client, channel, msg_ts, status_line)
        if ok:
            _status_msgs[skey] = (msg_ts, window_id, status_line)
        return

    ts = await send_message(client, channel, status_line, thread_ts=thread_ts)
    if ts:
        _status_msgs[skey] = (ts, window_id, status_line)


async def clear_status(
    user_id: str,
    thread_ts: str,
    client: AsyncWebClient,
    channel: str,
) -> None:
    """Remove status message and delivery tracking for a user/thread."""
    key = (user_id, thread_ts)
    _last_content_time.pop(key, None)
    existing = _status_msgs.pop(key, None)
    if existing:
        msg_ts, _wid, _text = existing
        await delete_message(client, channel, msg_ts)


async def start_status_polling(
    client: AsyncWebClient,
    get_channel_for_user: Callable[[str], Awaitable[str | None]],
) -> None:
    """Background loop polling terminal status for all thread-bound windows."""
    logger.info("Slack status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    while True:
        try:
            for uid, tid, wid in list(session_manager.iter_thread_bindings()):
                try:
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        session_manager.unbind_thread(uid, tid)
                        logger.info(
                            "Cleaned up stale binding: user=%s thread=%s window_id=%s",
                            uid,
                            tid,
                            wid,
                        )
                        continue

                    channel = await get_channel_for_user(uid)
                    if not channel:
                        continue

                    await update_status_for_window(client, uid, tid, wid, channel)
                except Exception as e:
                    logger.debug(
                        "Status update error for user %s thread %s: %s", uid, tid, e
                    )
        except Exception as e:
            logger.error("Status poll loop error: %s", e)

        await asyncio.sleep(STATUS_POLL_INTERVAL)


def take_status_ts(user_id: str, thread_ts: str) -> str | None:
    """Return and remove the current status message ts for a thread.

    Called by the queue worker when converting a status message to content
    (edit in-place). Returns None if no status message is tracked.
    """
    key = (user_id, thread_ts)
    existing = _status_msgs.pop(key, None)
    if existing:
        msg_ts, _wid, _text = existing
        return msg_ts
    return None


def register_status_ts(
    user_id: str, thread_ts: str, msg_ts: str, window_id: str, text: str
) -> None:
    """Register a newly-sent status message ts (called by queue worker after send)."""
    _status_msgs[(user_id, thread_ts)] = (msg_ts, window_id, text)
