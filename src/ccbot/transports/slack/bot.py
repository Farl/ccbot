"""Slack bot — Socket Mode app with event listeners.

Bridges Slack threads to Claude Code sessions via tmux windows.
Each Slack thread is bound to one tmux window running one Claude Code instance.
Uses Slack's Agents & Assistants framework: each assistant thread maps to one session.

Uses slack-bolt's AsyncApp with Socket Mode for real-time event handling.
"""

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp, AsyncAssistant

from ...config import config
from ...session import session_manager
from ...session_monitor import NewMessage, SessionMonitor
from ...tmux_manager import TmuxWindow, tmux_manager
from ...utils import ccbot_dir
from .handlers.directory_browser import (
    ACTION_DIR_CANCEL,
    ACTION_DIR_CONFIRM,
    ACTION_DIR_PAGE,
    ACTION_DIR_SELECT,
    ACTION_DIR_UP,
    ACTION_SESS_CANCEL,
    ACTION_SESS_NEW,
    ACTION_SESS_SELECT,
    build_directory_browser,
    build_session_picker,
    clear_browse_state,
    clear_session_picker_state,
    create_session_for_thread,
    get_browse_state,
    get_session_picker_state,
)
from .handlers.commands import dispatch_command, parse_text_command
from .handlers.interactive_ui import ACTION_NAV_PREFIX, handle_interactive_ui
from .handlers.message_queue import (
    build_response_parts,
    enqueue_content_message,
    shutdown_workers,
)
from .handlers.message_sender import send_message
from .handlers.status_polling import record_content_delivery, start_status_polling

logger = logging.getLogger(__name__)

app: AsyncApp | None = None
_dm_channels: dict[str, str] = {}
# Track thread_ts currently being processed by the assistant handler to prevent
# the regular message handler from double-processing the same message.
_assistant_processing: set[str] = set()


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


_IDLE_SHELLS: frozenset[str] = frozenset({"bash", "zsh", "sh", "fish", ""})


def _is_claude_running(window: TmuxWindow) -> bool:
    """Return True if Claude Code is the foreground process in the window."""
    return window.pane_current_command not in _IDLE_SHELLS


async def _restart_claude_in_window(window: TmuxWindow) -> None:
    """Restart Claude Code in an existing tmux window.

    Sends unset CLAUDECODE + claude command, then waits up to 10s for
    session_map to register the new session.
    """
    await tmux_manager.send_keys(window.window_id, "unset CLAUDECODE", enter=True)
    await tmux_manager.send_keys(window.window_id, config.claude_command, enter=True)
    logger.info("Restarting Claude in window %s", window.window_id)
    await session_manager.wait_for_session_map_entry(window.window_id, timeout=10.0)


async def _handle_user_message(
    user_id: str,
    channel: str,
    thread_ts: str,
    text: str,
    client: Any,
    say: Any = None,
) -> None:
    """Core message handler shared by assistant and regular message events."""
    if not config.is_user_allowed(user_id):
        return
    if not text:
        return

    # Check if this thread already has a session bound
    window_id = session_manager.resolve_window_for_thread(user_id, thread_ts)
    logger.info(
        "Thread lookup: user=%s thread_ts=%s -> window_id=%s (bindings=%s)",
        user_id,
        thread_ts,
        window_id,
        dict(session_manager.thread_bindings.get(user_id, {})),
    )
    if window_id:
        # Thread is bound — check window is alive before forwarding
        window = await tmux_manager.find_window_by_id(window_id)
        if not window:
            # Window died — unbind and fall through to directory browser
            logger.info(
                "Window %s no longer exists for user=%s thread=%s, unbinding",
                window_id,
                user_id,
                thread_ts,
            )
            session_manager.unbind_thread(user_id, thread_ts)
        else:
            # Window alive — check if Claude is still running, restart if not
            if not _is_claude_running(window):
                logger.info(
                    "Claude not running in window %s, restarting for user=%s",
                    window_id,
                    user_id,
                )
                await _restart_claude_in_window(window)
            success, msg = await session_manager.send_to_window(window_id, text)
            if not success:
                error_text = f"Failed: {msg}"
                if say is not None:
                    await say(text=error_text, channel=channel, thread_ts=thread_ts)
                else:
                    assert app is not None
                    await send_message(
                        app.client, channel, error_text, thread_ts=thread_ts
                    )
            return

    # No session for this thread — show directory browser
    logger.debug("Showing directory browser for user=%s thread=%s", user_id, thread_ts)
    try:
        # First send a placeholder, then update with browser keyed by msg_ts
        browser = build_directory_browser(user_id)
        resp = await client.chat_postMessage(
            channel=channel,
            text=browser["text"],
            blocks=browser["blocks"],
            thread_ts=thread_ts,
        )
        # Re-build with msg_ts so action callbacks can find the state
        msg_ts = resp.get("ts", "")
        if msg_ts:
            build_directory_browser(user_id, msg_ts=msg_ts, pending_text=text)
        logger.debug("Directory browser sent: ts=%s", msg_ts)
    except Exception as e:
        logger.error("Failed to send directory browser: %s", e)


async def _dispatch_incoming(
    user_id: str,
    channel: str,
    thread_ts: str,
    text: str,
    files: list[dict[str, Any]],
    client: Any,
    say: Any = None,
) -> None:
    """Route an incoming message to the appropriate handler.

    Shared by assistant.user_message, message event, and file_share fallback.
    """
    if files:
        await _handle_message_files(
            user_id, channel, thread_ts, text, files, client, say
        )
        return

    parsed = parse_text_command(text)
    if parsed:
        cmd, args = parsed
        handled = await dispatch_command(client, user_id, channel, thread_ts, cmd, args)
        if handled:
            return

    await _handle_user_message(user_id, channel, thread_ts, text, client, say)


def _register_handlers(slack_app: AsyncApp) -> None:
    """Register all event and action handlers on the app."""

    # --- Assistant handler for Agents & Assistants threads ---
    assistant = AsyncAssistant()

    @assistant.thread_started
    async def handle_thread_started(
        say: Any,
        set_suggested_prompts: Any,
    ) -> None:
        """Called when a user opens a new assistant thread."""
        await say("Send a message to start a Claude Code session.")
        await set_suggested_prompts(
            prompts=[
                {
                    "title": "Start session",
                    "message": "Start a new Claude Code session",
                },
            ]
        )

    @assistant.user_message
    async def handle_assistant_message(
        payload: dict[str, Any],
        client: Any,
        say: Any,
        set_status: Any,
    ) -> None:
        """Called when the user sends a message in an assistant thread."""
        user_id: str = payload.get("user", "")
        channel: str = payload.get("channel", "")
        thread_ts: str = payload.get("thread_ts") or payload.get("ts", "")
        text: str = payload.get("text", "")
        files: list[dict[str, Any]] = payload.get("files", [])

        logger.debug(
            "Assistant message: user=%s channel=%s thread=%s text=%s files=%d",
            user_id,
            channel,
            thread_ts,
            text[:50],
            len(files),
        )

        await set_status(status="processing...")
        _assistant_processing.add(thread_ts)
        try:
            await _dispatch_incoming(
                user_id, channel, thread_ts, text, files, client, say
            )
        finally:
            _assistant_processing.discard(thread_ts)
            await set_status("")

    slack_app.assistant(assistant)

    # --- Message handler for all DMs (assistant and non-assistant) ---
    @slack_app.event("message")
    async def handle_message(event: dict[str, Any], say: Any, client: Any) -> None:
        """Handles non-assistant DMs. Assistant threads go through handle_assistant_message."""
        if event.get("bot_id") is not None or event.get("subtype") == "bot_message":
            return
        user_id: str = event.get("user", "")
        channel: str = event.get("channel", "")
        thread_ts: str = event.get("thread_ts") or event.get("ts", "")
        # Skip if assistant handler is already processing this thread
        if thread_ts in _assistant_processing:
            return
        text: str = event.get("text", "")
        files: list[dict[str, Any]] = event.get("files", [])
        await _dispatch_incoming(user_id, channel, thread_ts, text, files, client, say)

    @slack_app.event({"type": "message", "subtype": "file_share"})
    async def handle_file_share(event: dict[str, Any], say: Any, client: Any) -> None:
        """Fallback for file_share events not caught by assistant handler."""
        user_id: str = event.get("user", "")
        channel: str = event.get("channel", "")
        thread_ts: str = event.get("thread_ts") or event.get("ts", "")
        text: str = event.get("text", "")
        files: list[dict[str, Any]] = event.get("files", [])
        if not files:
            return
        await _dispatch_incoming(user_id, channel, thread_ts, text, files, client, say)

    # --- Action handlers (work for both assistant and regular threads) ---

    @slack_app.action(re.compile(r"^dir_"))
    async def handle_dir_action(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        action = body["actions"][0]
        action_id: str = action["action_id"]
        user_id: str = body["user"]["id"]
        channel: str = body["channel"]["id"]
        message_ts: str = body["message"]["ts"]
        thread_ts: str | None = body["message"].get("thread_ts")

        logger.info(
            "Dir action: %s (user=%s, msg_ts=%s)", action_id, user_id, message_ts
        )

        state = get_browse_state(user_id, msg_ts=message_ts)
        if not state:
            logger.warning("No browse state for user=%s msg_ts=%s", user_id, message_ts)
            return
        current_path = Path(state["path"])

        try:
            if action_id.startswith(ACTION_DIR_SELECT):
                idx = int(action_id[len(ACTION_DIR_SELECT) :])
                dirs: list[str] = state["dirs"]
                if 0 <= idx < len(dirs):
                    new_path = current_path / dirs[idx]
                    browser = build_directory_browser(
                        user_id, new_path, msg_ts=message_ts
                    )
                    resp = await client.chat_update(
                        channel=channel,
                        ts=message_ts,
                        text=browser["text"],
                        blocks=browser["blocks"],
                    )
                    logger.info("chat_update ok=%s for %s", resp.get("ok"), action_id)
            elif action_id == ACTION_DIR_UP:
                browser = build_directory_browser(
                    user_id, current_path.parent, msg_ts=message_ts
                )
                resp = await client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text=browser["text"],
                    blocks=browser["blocks"],
                )
                logger.info("chat_update ok=%s for %s", resp.get("ok"), action_id)
            elif action_id.startswith(ACTION_DIR_PAGE):
                page = int(action_id[len(ACTION_DIR_PAGE) :])
                browser = build_directory_browser(
                    user_id, current_path, page, msg_ts=message_ts
                )
                resp = await client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text=browser["text"],
                    blocks=browser["blocks"],
                )
                logger.info("chat_update ok=%s for %s", resp.get("ok"), action_id)
            elif action_id == ACTION_DIR_CONFIRM:
                pending_text = state.get("pending_text")
                clear_browse_state(user_id, msg_ts=message_ts)
                effective_ts = thread_ts or message_ts

                # Check for existing sessions in this directory
                sessions = await session_manager.list_sessions_for_directory(
                    str(current_path)
                )
                if sessions:
                    picker = build_session_picker(
                        user_id,
                        sessions,
                        msg_ts=message_ts,
                        cwd=str(current_path),
                        pending_text=pending_text,
                        thread_ts=effective_ts,
                    )
                    await client.chat_update(
                        channel=channel,
                        ts=message_ts,
                        text=picker["text"],
                        blocks=picker["blocks"],
                    )
                    return

                # No existing sessions — create new window
                if not thread_ts:
                    resp = await client.chat_postMessage(
                        channel=channel, text=f"\U0001f680 Session: {current_path.name}"
                    )
                    effective_ts = resp.get("ts") or message_ts
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
                    display_path = str(current_path).replace(str(Path.home()), "~")
                    try:
                        await client.assistant_threads_setTitle(
                            channel_id=channel,
                            thread_ts=effective_ts,
                            title=display_path,
                        )
                    except Exception:
                        logger.debug("Failed to set thread title", exc_info=True)
                    if pending_text:
                        await _dispatch_incoming(
                            user_id,
                            channel,
                            effective_ts,
                            pending_text,
                            [],
                            client,
                        )
                else:
                    await client.chat_update(
                        channel=channel,
                        ts=message_ts,
                        text="\u274c Failed to create session",
                        blocks=[],
                    )
            elif action_id == ACTION_DIR_CANCEL:
                clear_browse_state(user_id, msg_ts=message_ts)
                await client.chat_update(
                    channel=channel, ts=message_ts, text="Cancelled.", blocks=[]
                )
        except Exception as e:
            logger.error(
                "Dir action error: %s (action=%s)", e, action_id, exc_info=True
            )

    @slack_app.action(re.compile(r"^sess_"))
    async def handle_sess_action(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        action = body["actions"][0]
        action_id: str = action["action_id"]
        user_id: str = body["user"]["id"]
        channel: str = body["channel"]["id"]
        message_ts: str = body["message"]["ts"]

        state = get_session_picker_state(user_id, msg_ts=message_ts)
        if not state:
            return

        sessions = state.get("sessions", [])
        cwd: str = state.get("cwd", "")
        pending_text: str | None = state.get("pending_text")
        thread_ts: str = state.get("thread_ts", message_ts)

        try:
            if action_id.startswith(ACTION_SESS_SELECT):
                idx = int(action_id[len(ACTION_SESS_SELECT) :])
                if idx < 0 or idx >= len(sessions):
                    return
                session = sessions[idx]
                clear_session_picker_state(user_id, msg_ts=message_ts)
                window_id = await create_session_for_thread(
                    user_id,
                    thread_ts,
                    cwd,
                    resume_session_id=session.session_id,
                )
                if window_id:
                    await client.chat_update(
                        channel=channel,
                        ts=message_ts,
                        text=f"\u2705 Resumed session {session.session_id[:8]}",
                        blocks=[],
                    )
                    display_path = cwd.replace(str(Path.home()), "~")
                    try:
                        await client.assistant_threads_setTitle(
                            channel_id=channel, thread_ts=thread_ts, title=display_path
                        )
                    except Exception:
                        logger.debug("Failed to set thread title", exc_info=True)
                    if pending_text:
                        await _dispatch_incoming(
                            user_id,
                            channel,
                            thread_ts,
                            pending_text,
                            [],
                            client,
                        )
                else:
                    await client.chat_update(
                        channel=channel,
                        ts=message_ts,
                        text="\u274c Failed to resume session",
                        blocks=[],
                    )

            elif action_id == ACTION_SESS_NEW:
                clear_session_picker_state(user_id, msg_ts=message_ts)
                window_id = await create_session_for_thread(user_id, thread_ts, cwd)
                if window_id:
                    await client.chat_update(
                        channel=channel,
                        ts=message_ts,
                        text=f"\u2705 New session: {cwd}",
                        blocks=[],
                    )
                    display_path = cwd.replace(str(Path.home()), "~")
                    try:
                        await client.assistant_threads_setTitle(
                            channel_id=channel, thread_ts=thread_ts, title=display_path
                        )
                    except Exception:
                        logger.debug("Failed to set thread title", exc_info=True)
                    if pending_text:
                        await _dispatch_incoming(
                            user_id,
                            channel,
                            thread_ts,
                            pending_text,
                            [],
                            client,
                        )
                else:
                    await client.chat_update(
                        channel=channel,
                        ts=message_ts,
                        text="\u274c Failed to create session",
                        blocks=[],
                    )

            elif action_id == ACTION_SESS_CANCEL:
                clear_session_picker_state(user_id, msg_ts=message_ts)
                await client.chat_update(
                    channel=channel, ts=message_ts, text="Cancelled.", blocks=[]
                )

        except Exception as e:
            logger.error(
                "Sess action error: %s (action=%s)", e, action_id, exc_info=True
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

        user_id: str = body["user"]["id"]
        channel_id: str = body["channel"]["id"]
        msg_ts: str = body["message"].get("ts", "")
        thread_ts: str = body["message"].get("thread_ts", "")

        # Determine if this button came from an interactive UI message or
        # a screenshot message, so we don't accidentally cross-trigger.
        from .handlers.interactive_ui import _interactive_msgs
        from .handlers.commands import _screenshot_states

        ikey = (user_id, thread_ts)
        is_interactive_btn = msg_ts == _interactive_msgs.get(ikey)
        sstate = _screenshot_states.get(ikey)
        is_screenshot_btn = sstate is not None and msg_ts == sstate.get("nav_msg_ts")

        if key_name == "refresh" or tmux_key:
            import asyncio

            if tmux_key:
                await asyncio.sleep(0.5)
            pane_text = await tmux_manager.capture_pane(window_id)
            if pane_text:
                if is_interactive_btn or not is_screenshot_btn:
                    # Button came from interactive UI — only update interactive UI
                    await handle_interactive_ui(
                        client, user_id, thread_ts, window_id, channel_id, pane_text
                    )
                else:
                    # Button came from screenshot — only refresh screenshot
                    from .handlers.commands import capture_and_upload

                    await capture_and_upload(
                        client, user_id, channel_id, thread_ts, window_id
                    )

    @slack_app.command("/esc")
    async def slash_esc(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        await dispatch_command(
            client,
            body["user_id"],
            body["channel_id"],
            body.get("thread_ts", ""),
            "esc",
            [],
        )

    @slack_app.command("/unbind")
    async def slash_unbind(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        await dispatch_command(
            client,
            body["user_id"],
            body["channel_id"],
            body.get("thread_ts", ""),
            "unbind",
            [],
        )

    @slack_app.command("/screenshot")
    async def slash_screenshot(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        await dispatch_command(
            client,
            body["user_id"],
            body["channel_id"],
            body.get("thread_ts", ""),
            "screenshot",
            [],
        )

    @slack_app.command("/history")
    async def slash_history(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        args = body.get("text", "").split()
        await dispatch_command(
            client,
            body["user_id"],
            body["channel_id"],
            body.get("thread_ts", ""),
            "history",
            args,
        )

    @slack_app.action(re.compile(r"^hist_"))
    async def handle_hist_action(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        action = body["actions"][0]
        action_id: str = action["action_id"]
        channel: str = body["channel"]["id"]
        message_ts: str = body["message"]["ts"]
        thread_ts: str = body["message"].get("thread_ts", "")
        user_id: str = body["user"]["id"]

        from .handlers.history import parse_history_action_id, send_history

        try:
            page, window_id = parse_history_action_id(action_id)
        except (ValueError, IndexError):
            return

        await send_history(
            client,
            user_id,
            channel,
            thread_ts,
            window_id,
            page=page,
            edit_ts=message_ts,
        )


_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


async def _download_slack_file(url: str) -> bytes | None:
    """Download a Slack file using the bot token (max 20 MB)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.get(
                url,
                headers={"Authorization": f"Bearer {config.slack_bot_token}"},
            )
            resp.raise_for_status()
            if len(resp.content) > _MAX_FILE_SIZE:
                logger.warning(
                    "Slack file too large (%d bytes), skipping", len(resp.content)
                )
                return None
            return resp.content
    except Exception as e:
        logger.error("Failed to download Slack file: %s", e)
        return None


# --- Image directory for incoming photos (matches Telegram convention) ---
_IMAGES_DIR = ccbot_dir() / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)


async def _handle_message_files(
    user_id: str,
    channel: str,
    thread_ts: str,
    text: str,
    files: list[dict[str, Any]],
    client: Any,
    say: Any = None,
) -> None:
    """Process file attachments from a Slack message event.

    Images are saved to ~/.ccbot/images/ and forwarded as file paths.
    Audio is transcribed and forwarded as text.
    Any accompanying text is included alongside the file.
    """
    if not config.is_user_allowed(user_id):
        return

    logger.info(
        "Processing %d file(s) from user=%s thread=%s",
        len(files),
        user_id,
        thread_ts,
    )

    window_id = session_manager.resolve_window_for_thread(user_id, thread_ts)
    if not window_id:
        # No session bound — forward text only to trigger directory browser
        if text:
            await _handle_user_message(user_id, channel, thread_ts, text, client, say)
        return

    for file_info in files:
        mimetype: str = file_info.get("mimetype", "")
        download_url: str = file_info.get("url_private_download", "")
        if not download_url:
            continue

        file_bytes = await _download_slack_file(download_url)
        if file_bytes is None:
            continue

        if mimetype.startswith("image/"):
            filetype = file_info.get("filetype", "png")
            file_id = file_info.get("id", "unknown")
            filename = f"{int(time.time())}_{file_id}.{filetype}"
            file_path = _IMAGES_DIR / filename
            file_path.write_bytes(file_bytes)

            if text:
                msg_text = f"{text}\n\n(image attached: {file_path})"
            else:
                msg_text = f"(image attached: {file_path})"

            ok, err = await session_manager.send_to_window(window_id, msg_text)
            if not ok:
                logger.warning("Failed to forward image to Claude: %s", err)
            # Only include text with the first file
            text = ""

        elif mimetype.startswith("audio/") or mimetype in ("video/mp4",):
            await _handle_audio(user_id, channel, thread_ts, file_bytes, client)


async def _handle_audio(
    user_id: str,
    channel: str,
    thread_ts: str,
    audio_bytes: bytes,
    client: Any,
) -> None:
    """Transcribe audio and forward the text to Claude."""
    from ...transcribe import transcribe_voice

    try:
        text = await transcribe_voice(audio_bytes)
    except Exception as e:
        logger.error("Transcription failed: %s", e)
        assert app is not None
        await send_message(
            app.client, channel, f"Transcription failed: {e}", thread_ts=thread_ts
        )
        return
    await _handle_user_message(user_id, channel, thread_ts, text, client)


async def handle_new_message(msg: NewMessage) -> None:
    """Session monitor callback — deliver Claude messages to Slack threads."""
    users = await session_manager.find_users_for_session(msg.session_id)
    for user_id, window_id, thread_id in users:
        channel = await _resolve_dm_channel(user_id)
        if not channel:
            continue
        assert app is not None

        record_content_delivery(user_id, thread_id)

        parts = build_response_parts(
            msg.text,
            msg.is_complete,
            msg.content_type,
            msg.role,
        )

        if msg.is_complete:
            await enqueue_content_message(
                client=app.client,
                user_id=user_id,
                channel=channel,
                thread_ts=thread_id,
                window_id=window_id,
                parts=parts,
                tool_use_id=msg.tool_use_id,
                content_type=msg.content_type,
                text=msg.text,
            )

            # Track read offset — prevents message replay on restart
            session = await session_manager.resolve_session_for_window(window_id)
            if session and session.file_path:
                try:
                    file_size = Path(session.file_path).stat().st_size
                    session_manager.update_user_window_offset(
                        user_id, window_id, file_size
                    )
                except OSError:
                    pass


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
        try:
            await handler.start_async()
        finally:
            await shutdown_workers()

    asyncio.run(_run())
