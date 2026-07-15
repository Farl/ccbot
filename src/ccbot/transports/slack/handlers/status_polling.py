"""Terminal status line polling for Slack transport.

Background task that polls terminal status lines for all thread-bound windows
at 1-second intervals. Detects interactive UIs and delegates to the interactive
UI handler. The working status ("Thinking…/Ideating…") is shown exclusively via
the Slack Assistant thread status (the native "thinking" indicator) — never as a
chat message, which would clutter the thread and auto-clear the indicator.

Key state:
  _last_thread_status: (user_id, thread_ts) -> last setStatus text (dedups the
    empty/clear case only; non-empty status is re-asserted every poll)
"""

import asyncio
import logging
import math
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

logger = logging.getLogger(__name__)

STATUS_POLL_INTERVAL = 1.0  # seconds

# Idle polls to hold the native "thinking" status before clearing it. The
# trailing reply is delivered by the session monitor (config.monitor_poll_interval,
# ~2s) plus queue latency, whereas this loop detects an idle pane within one
# STATUS_POLL_INTERVAL (~1s). Clearing on the first idle poll makes the indicator
# blink out a beat before the last message lands. Holding a few extra idle polls
# lets that message post first — Slack auto-clears the status on any posted
# message — so the indicator and the final message hand off together; only when
# NO trailing message arrives do we clear ourselves after the grace. Derived from
# the monitor cadence so it adapts if that interval changes.
STATUS_CLEAR_GRACE_POLLS = (
    math.ceil(config.monitor_poll_interval / STATUS_POLL_INTERVAL) + 2
)

# Seconds to suppress new interactive UI after the last session monitor delivery.
# Gives the session monitor time to flush all pending tool/response messages first.
_CONTENT_SETTLE_TIME = 3.0

# (user_id, thread_ts) -> monotonic time of last content delivery
_last_content_time: dict[tuple[str, str], float] = {}

# (user_id, thread_ts) -> last text passed to setStatus (dedups clears only)
_last_thread_status: dict[tuple[str, str], str] = {}

# (user_id, thread_ts) -> consecutive idle polls seen while the pane has no
# active status; gates the delayed clear so the indicator doesn't vanish before
# the trailing message is delivered.
_status_clear_grace: dict[tuple[str, str], int] = {}

# Callback set by start_status_polling; signature: (channel, thread_ts, text) -> None
_set_thread_status: Callable[[str, str, str], Awaitable[None]] | None = None


async def _sync_thread_status(
    user_id: str, thread_ts: str, channel: str, text: str
) -> None:
    """Set Slack Assistant thread status.

    Slack auto-clears the assistant thread status whenever the app posts a
    message to the thread, and again after a 2-minute idle timeout (see
    assistant.threads.setStatus docs). So a non-empty "thinking" status must be
    re-asserted on EVERY poll — caching "we already set this text" would leave
    the indicator blank the moment the first content/tool/status message clears
    it, which is why the indicator used to appear only briefly then vanish.

    Only the empty (clear) case is deduped: once cleared, repeatedly sending
    setStatus("") would just spam the API for idle threads with no visible
    effect. Re-asserts stay within the method's 600/min limit (≤1/s per active
    thread, matching the poll interval).
    """
    if _set_thread_status is None:
        return
    key = (user_id, thread_ts)
    if not text and _last_thread_status.get(key) == "":
        return  # already cleared — skip redundant clear
    _last_thread_status[key] = text
    await _set_thread_status(channel, thread_ts, text)


# Marker stored by mark_status_active. Only the emptiness of a cache entry is
# ever read (the clear-dedup in _sync_thread_status fires solely when the entry
# reads ""), so any non-empty value works — it need NOT match the text handed to
# the framework's set_status().
_ACTIVE_MARKER = "…"


def mark_status_active(user_id: str, thread_ts: str) -> None:
    """Record that a native thread status was set OUTSIDE this module.

    The assistant handler sets the initial indicator via the Slack framework's
    set_status(), which hits the API directly and bypasses `_sync_thread_status`,
    leaving the `_last_thread_status` cache stale. If the cache still reads ""
    (from the prior interaction's clear), the poller's idle clear gets deduped and
    the indicator sticks forever — visible after a bare `/clear`, which posts no
    reply for Slack to auto-clear on. Marking the entry non-empty keeps the cache
    truthful so that clear is not falsely deduped.
    """
    _last_thread_status[(user_id, thread_ts)] = _ACTIVE_MARKER


def forget_thread(user_id: str, thread_ts: str) -> None:
    """Drop all per-thread status tracking.

    Used by the poller teardown on unbind, and by the assistant handler when a
    thread ends without ever binding a session — the poller only reclaims entries
    for bound threads, so an unbound thread's seeded marker would otherwise leak.
    """
    key = (user_id, thread_ts)
    _last_content_time.pop(key, None)
    _last_thread_status.pop(key, None)
    _status_clear_grace.pop(key, None)


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
        await clear_status(user_id, thread_ts, channel)
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
    if not config.show_status:
        # Indicator disabled by config — clear immediately, no grace.
        await clear_status(user_id, thread_ts, channel)
        return
    if not status_line:
        # Idle pane. Don't clear on the first idle poll: the trailing reply is
        # delivered by the slower session monitor (see STATUS_CLEAR_GRACE_POLLS),
        # so clearing now would blink the indicator out just before that message
        # lands. Hold for a few idle polls — the message posts within that window
        # and Slack auto-clears the status on post, a seamless hand-off. Only if
        # no message arrives do we clear ourselves once the grace elapses.
        miss = _status_clear_grace.get(ikey, 0) + 1
        _status_clear_grace[ikey] = miss
        if miss < STATUS_CLEAR_GRACE_POLLS:
            return
        await clear_status(user_id, thread_ts, channel)
        return

    # Active status — reset the idle grace so the next idle starts fresh.
    _status_clear_grace.pop(ikey, None)

    # NOTE: silent mode does NOT suppress this indicator. It's a lightweight
    # native status (not a chat message), and while silent hides the noisy
    # thinking/tool messages, the "still working…" indicator is exactly the
    # quiet signal the user relies on to see the session is alive.

    # Slack's native assistant thread status is the SOLE thinking indicator.
    # We deliberately never post "Thinking…/Ideating…" as a chat message: it
    # would clutter the thread and, per Slack, posting any message auto-clears
    # the status — so the message would fight the very indicator it duplicates.
    await _sync_thread_status(user_id, thread_ts, channel, status_line)


async def clear_status(
    user_id: str,
    thread_ts: str,
    channel: str,
) -> None:
    """Clear the native thread status indicator and drop content tracking.

    Leaves the `_last_thread_status` entry as "" (managed by
    `_sync_thread_status`) so repeated idle polls don't re-send setStatus("").
    Full per-thread teardown on unbind happens in the polling loop.
    """
    _last_content_time.pop((user_id, thread_ts), None)
    _status_clear_grace.pop((user_id, thread_ts), None)
    await _sync_thread_status(user_id, thread_ts, channel, "")


async def start_status_polling(
    client: AsyncWebClient,
    get_channel_for_user: Callable[[str], Awaitable[str | None]],
    set_thread_status: Callable[[str, str, str], Awaitable[None]] | None = None,
) -> None:
    """Background loop polling terminal status for all thread-bound windows."""
    global _set_thread_status
    _set_thread_status = set_thread_status
    logger.info("Slack status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    while True:
        try:
            for uid, tid, wid in list(session_manager.iter_thread_bindings()):
                try:
                    # Only unbind when the window is *definitively* gone. A
                    # transient tmux query failure (window_exists → None) must
                    # not drop a live binding — that's what made sessions "unbind
                    # themselves" under heavy tmux contention.
                    exists = await tmux_manager.window_exists(wid)
                    if exists is None:
                        continue  # can't tell right now — keep binding, retry next tick
                    if not exists:
                        session_manager.unbind_thread(uid, tid)
                        forget_thread(uid, tid)  # drop in-memory tracking
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
