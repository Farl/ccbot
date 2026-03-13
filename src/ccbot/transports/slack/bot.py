"""Slack bot — Socket Mode app with event listeners.

Bridges Slack threads to Claude Code sessions via tmux windows.
Each Slack thread is bound to one tmux window running one Claude Code instance.
Top-level messages trigger the directory browser for session creation.

Uses slack-bolt's AsyncApp with Socket Mode for real-time event handling.
"""

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from ...config import config
from ...session import session_manager
from ...session_monitor import NewMessage, SessionMonitor
from ...tmux_manager import tmux_manager
from .handlers.directory_browser import (
    ACTION_DIR_CANCEL,
    ACTION_DIR_CONFIRM,
    ACTION_DIR_PAGE,
    ACTION_DIR_SELECT,
    ACTION_DIR_UP,
    build_directory_browser,
    clear_browse_state,
    create_session_for_thread,
    get_browse_state,
)
from .handlers.interactive_ui import ACTION_NAV_PREFIX, handle_interactive_ui
from .handlers.message_sender import send_long_message
from .handlers.status_polling import start_status_polling

logger = logging.getLogger(__name__)

app: AsyncApp | None = None
_dm_channels: dict[str, str] = {}


def _create_app() -> AsyncApp:
    """Create the Slack Bolt async app."""
    return AsyncApp(token=config.slack_bot_token)


async def _resolve_dm_channel(user_id: str) -> str | None:
    """Open or retrieve a DM channel with a user."""
    if user_id in _dm_channels:
        return _dm_channels[user_id]
    try:
        assert app is not None
        resp = await app.client.conversations_open(users=[user_id])
        channel_id: str = resp["channel"]["id"]  # type: ignore[index]
        _dm_channels[user_id] = channel_id
        return channel_id
    except Exception as e:
        logger.error("Failed to open DM with %s: %s", user_id, e)
        return None


def _is_bot_message(event: dict[str, Any]) -> bool:
    """Check if a message event is from a bot."""
    return event.get("bot_id") is not None or event.get("subtype") == "bot_message"


def _register_handlers(slack_app: AsyncApp) -> None:
    """Register all event and action handlers on the app."""

    @slack_app.event("message")
    async def handle_message(
        event: dict[str, Any],
        say: Any,
        client: Any,
    ) -> None:
        if _is_bot_message(event):
            return
        user_id: str = event.get("user", "")
        if not config.is_user_allowed(user_id):
            return
        channel: str = event.get("channel", "")
        thread_ts: str | None = event.get("thread_ts")
        text: str = event.get("text", "")
        if not text:
            return

        if not thread_ts:
            # Top-level message: show directory browser
            browser = build_directory_browser(user_id)
            await client.chat_postMessage(
                channel=channel,
                text=browser["text"],
                blocks=browser["blocks"],
            )
            return

        window_id = session_manager.resolve_window_for_thread(user_id, thread_ts)
        if not window_id:
            await say(
                text="This thread is not bound to a session. "
                "Send a top-level message to start one.",
                channel=channel,
                thread_ts=thread_ts,
            )
            return

        success, msg = await session_manager.send_to_window(window_id, text)
        if not success:
            await say(text=f"Failed: {msg}", channel=channel, thread_ts=thread_ts)

    @slack_app.action(re.compile(r"^dir_"))
    async def handle_dir_action(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        action = body["actions"][0]
        action_id: str = action["action_id"]
        user_id: str = body["user"]["id"]
        channel: str = body["channel"]["id"]
        message_ts: str = body["message"]["ts"]
        thread_ts: str | None = body["message"].get("thread_ts")

        state = get_browse_state(user_id)
        if not state:
            return
        current_path = Path(state["path"])

        if action_id.startswith(ACTION_DIR_SELECT):
            idx = int(action_id[len(ACTION_DIR_SELECT) :])
            dirs: list[str] = state["dirs"]
            if 0 <= idx < len(dirs):
                new_path = current_path / dirs[idx]
                browser = build_directory_browser(user_id, new_path)
                await client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text=browser["text"],
                    blocks=browser["blocks"],
                )
        elif action_id == ACTION_DIR_UP:
            browser = build_directory_browser(user_id, current_path.parent)
            await client.chat_update(
                channel=channel,
                ts=message_ts,
                text=browser["text"],
                blocks=browser["blocks"],
            )
        elif action_id.startswith(ACTION_DIR_PAGE):
            page = int(action_id[len(ACTION_DIR_PAGE) :])
            browser = build_directory_browser(user_id, current_path, page)
            await client.chat_update(
                channel=channel,
                ts=message_ts,
                text=browser["text"],
                blocks=browser["blocks"],
            )
        elif action_id == ACTION_DIR_CONFIRM:
            clear_browse_state(user_id)
            effective_ts = thread_ts
            if not effective_ts:
                resp = await client.chat_postMessage(
                    channel=channel,
                    text=f"\U0001f680 Session: {current_path.name}",
                )
                effective_ts = resp["ts"] or message_ts
            window_id = await create_session_for_thread(
                user_id, effective_ts, str(current_path)
            )
            if window_id:
                await client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text=f"\u2705 Session created: {current_path.name}",
                    blocks=[],
                )
            else:
                await client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text="\u274c Failed to create session",
                    blocks=[],
                )
        elif action_id == ACTION_DIR_CANCEL:
            clear_browse_state(user_id)
            await client.chat_update(
                channel=channel, ts=message_ts, text="Cancelled.", blocks=[]
            )

    @slack_app.action("noop")
    async def handle_noop(ack: Any) -> None:
        await ack()

    @slack_app.action(re.compile(r"^nav_"))
    async def handle_nav_action(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        action = body["actions"][0]
        action_id: str = action["action_id"]
        # Format: nav_{key}_{window_id}
        suffix = action_id[len(ACTION_NAV_PREFIX) :]
        parts = suffix.rsplit("_", 1)
        if len(parts) != 2:
            return
        key_name, window_id = parts

        key_map = {
            "up": "Up",
            "down": "Down",
            "left": "Left",
            "right": "Right",
            "enter": "Enter",
            "esc": "Escape",
            "space": "Space",
            "tab": "Tab",
        }
        tmux_key = key_map.get(key_name)
        if tmux_key:
            # send_keys with literal=False interprets special key names
            await tmux_manager.send_keys(
                window_id, tmux_key, enter=False, literal=False
            )

        if key_name == "refresh":
            user_id: str = body["user"]["id"]
            channel_id: str = body["channel"]["id"]
            thread_ts: str = body["message"].get("thread_ts", "")
            pane_text = await tmux_manager.capture_pane(window_id)
            if pane_text:
                await handle_interactive_ui(
                    client, user_id, thread_ts, window_id, channel_id, pane_text
                )


async def handle_new_message(msg: NewMessage) -> None:
    """Session monitor callback — deliver Claude messages to Slack threads."""
    users = await session_manager.find_users_for_session(msg.session_id)
    for user_id, _window_id, thread_id in users:
        channel = await _resolve_dm_channel(user_id)
        if not channel:
            continue
        assert app is not None
        await send_long_message(app.client, channel, msg.text, thread_ts=thread_id)


def run_slack_bot() -> None:
    """Start Slack bot with Socket Mode."""
    global app
    app = _create_app()
    _register_handlers(app)

    async def _run() -> None:
        assert app is not None
        await session_manager.resolve_stale_ids()

        monitor = SessionMonitor()
        monitor.set_message_callback(handle_new_message)
        monitor.start()

        asyncio.create_task(start_status_polling(app.client, _resolve_dm_channel))

        logger.info("Starting Slack bot in Socket Mode...")
        handler = AsyncSocketModeHandler(app, config.slack_app_token)
        await handler.start_async()

    asyncio.run(_run())
