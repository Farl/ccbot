"""Unified command dispatch for Slack transport.

Handles both text prefix commands (!esc) and Slack slash commands (/esc).
Both call dispatch_command() — the same logic handles both.

Supported commands:
  esc        — Send Escape key to the bound tmux window
  unbind     — Kill window and unbind thread
  screenshot — Capture terminal as PNG and upload to Slack
  history    — Show paginated session history
  bind       — Open directory browser to bind a session (without forwarding text)
  silent     — Toggle silent mode (suppress notifications) for the bound session
  help       — List all supported commands

Import depth from this file (src/ccbot/transports/slack/handlers/):
  .... = 4 levels up = src/ccbot/
"""

import logging
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from ....session import session_manager
from ....tmux_manager import tmux_manager
from .message_sender import delete_message, send_message

logger = logging.getLogger(__name__)

# Per-thread screenshot state: (user_id, thread_ts) -> {file_msg_ts, nav_msg_ts, window_id}
_screenshot_states: dict[tuple[str, str], dict[str, Any]] = {}


async def clear_screenshot_state(
    client: AsyncWebClient,
    user_id: str,
    channel: str,
    thread_ts: str,
) -> None:
    """Delete screenshot + nav buttons from a thread if present."""
    skey = (user_id, thread_ts)
    state = _screenshot_states.pop(skey, None)
    if not state:
        return
    file_ts = state.get("file_msg_ts")
    nav_ts = state.get("nav_msg_ts")
    if file_ts:
        await delete_message(client, channel, file_ts)
    if nav_ts:
        await delete_message(client, channel, nav_ts)


def parse_text_command(text: str) -> tuple[str, list[str]] | None:
    """Parse a !command string.

    Returns (command_name, args) or None if not a command.
    """
    text = text.strip()
    if not text.startswith("!") or len(text) < 2:
        return None
    parts = text[1:].split()
    if not parts:
        return None
    return parts[0].lower(), parts[1:]


async def dispatch_command(
    client: AsyncWebClient,
    user_id: str,
    channel: str,
    thread_ts: str,
    cmd: str,
    args: list[str],
) -> bool:
    """Dispatch a command. Returns True if handled."""
    if cmd == "esc":
        await _cmd_esc(client, user_id, channel, thread_ts)
    elif cmd == "unbind":
        await _cmd_unbind(client, user_id, channel, thread_ts)
    elif cmd == "screenshot":
        await _cmd_screenshot(client, user_id, channel, thread_ts)
    elif cmd == "history":
        page = int(args[0]) - 1 if args and args[0].isdigit() else -1
        await _cmd_history(client, user_id, channel, thread_ts, page)
    elif cmd == "bind":
        await _cmd_bind(client, user_id, channel, thread_ts)
    elif cmd == "silent":
        await _cmd_silent(client, user_id, channel, thread_ts, args)
    elif cmd == "help":
        await _cmd_help(client, channel, thread_ts)
    else:
        return False
    return True


def _resolve_window(user_id: str, thread_ts: str) -> str | None:
    """Return bound window_id or None."""
    return session_manager.resolve_window_for_thread(user_id, thread_ts)


async def _cmd_esc(
    client: AsyncWebClient, user_id: str, channel: str, thread_ts: str
) -> None:
    """Send Escape key to the bound tmux window."""
    window_id = _resolve_window(user_id, thread_ts)
    if not window_id:
        await send_message(
            client, channel, "No active session in this thread.", thread_ts=thread_ts
        )
        return
    await tmux_manager.send_keys(window_id, "Escape", enter=False, literal=False)
    await send_message(client, channel, "↩ Escape sent.", thread_ts=thread_ts)


async def _cmd_unbind(
    client: AsyncWebClient, user_id: str, channel: str, thread_ts: str
) -> None:
    """Kill window and unbind thread."""
    window_id = _resolve_window(user_id, thread_ts)
    if not window_id:
        await send_message(
            client, channel, "No active session in this thread.", thread_ts=thread_ts
        )
        return
    from .cleanup import cleanup_thread

    await cleanup_thread(user_id, thread_ts, client, channel, kill_window=True)
    await send_message(client, channel, "Session unbound.", thread_ts=thread_ts)


async def _upload_screenshot(
    client: AsyncWebClient, channel: str, thread_ts: str, png_bytes: bytes
) -> str | None:
    """Upload a PNG screenshot to Slack.

    Returns the file message ts (for later deletion) or None.
    """
    import httpx

    upload_resp = await client.files_getUploadURLExternal(
        filename="screenshot.png",
        length=len(png_bytes),
    )
    upload_url: str = upload_resp["upload_url"]  # type: ignore[index]
    file_id: str = upload_resp["file_id"]  # type: ignore[index]

    async with httpx.AsyncClient() as http:
        await http.post(
            upload_url,
            content=png_bytes,
            headers={"Content-Type": "image/png"},
        )

    await client.files_completeUploadExternal(
        files=[{"id": file_id, "title": "Terminal screenshot"}],
        channel_id=channel,
        thread_ts=thread_ts,
    )

    # Get the file message ts by scanning thread replies for the uploaded file
    import asyncio

    await asyncio.sleep(1.5)
    try:
        replies = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=10, oldest=thread_ts
        )
        msgs = replies.get("messages", [])  # type: ignore[union-attr]
        # Walk backwards to find the most recent message containing our file_id
        for msg in reversed(msgs):
            msg_files = msg.get("files", [])
            for f in msg_files:
                if f.get("id") == file_id:
                    return msg.get("ts")  # type: ignore[no-any-return]
    except Exception:
        logger.debug("Could not retrieve file message ts", exc_info=True)
    return None


async def capture_and_upload(
    client: AsyncWebClient,
    user_id: str,
    channel: str,
    thread_ts: str,
    window_id: str,
) -> None:
    """Capture terminal, delete old screenshot if any, upload new one + nav buttons.

    Used by both _cmd_screenshot and the nav button refresh handler.
    """
    from ....screenshot import text_to_image

    pane_text = await tmux_manager.capture_pane(window_id, with_ansi=True)
    if not pane_text:
        return

    try:
        png_bytes = await text_to_image(pane_text, with_ansi=True)
    except Exception as e:
        logger.debug("Screenshot render failed: %s", e)
        return

    skey = (user_id, thread_ts)
    old_state = _screenshot_states.get(skey)

    # Delete old screenshot file message and nav buttons message
    if old_state:
        old_file_ts = old_state.get("file_msg_ts")
        old_nav_ts = old_state.get("nav_msg_ts")
        if old_file_ts:
            await delete_message(client, channel, old_file_ts)
        if old_nav_ts:
            await delete_message(client, channel, old_nav_ts)

    # Upload new screenshot
    try:
        file_msg_ts = await _upload_screenshot(client, channel, thread_ts, png_bytes)
    except Exception as e:
        logger.debug("Screenshot upload failed: %s", e)
        _screenshot_states.pop(skey, None)
        return

    # Send navigation keyboard
    from .interactive_ui import build_nav_keyboard

    nav_blocks = build_nav_keyboard(window_id)
    nav_msg_ts = await send_message(
        client,
        channel,
        "Terminal controls:",
        thread_ts=thread_ts,
        blocks=nav_blocks,
    )

    # Track state for delete-on-refresh
    _screenshot_states[skey] = {
        "file_msg_ts": file_msg_ts,
        "nav_msg_ts": nav_msg_ts,
        "window_id": window_id,
    }
    logger.debug(
        "Screenshot state: file_msg_ts=%s, nav_msg_ts=%s",
        file_msg_ts,
        nav_msg_ts,
    )


async def _cmd_screenshot(
    client: AsyncWebClient, user_id: str, channel: str, thread_ts: str
) -> None:
    """Capture terminal as PNG and upload to Slack with navigation buttons."""
    window_id = _resolve_window(user_id, thread_ts)
    if not window_id:
        await send_message(
            client, channel, "No active session in this thread.", thread_ts=thread_ts
        )
        return

    pane_text = await tmux_manager.capture_pane(window_id, with_ansi=True)
    if not pane_text:
        await send_message(
            client, channel, "Failed to capture terminal.", thread_ts=thread_ts
        )
        return

    try:
        from ....screenshot import text_to_image

        png_bytes = await text_to_image(pane_text, with_ansi=True)
    except Exception as e:
        logger.error("Screenshot render failed: %s", e)
        await send_message(
            client, channel, f"Screenshot failed: {e}", thread_ts=thread_ts
        )
        return

    skey = (user_id, thread_ts)

    try:
        file_msg_ts = await _upload_screenshot(client, channel, thread_ts, png_bytes)
    except Exception as e:
        logger.error("Screenshot upload failed: %s", e)
        await send_message(client, channel, f"Upload failed: {e}", thread_ts=thread_ts)
        return

    # Send navigation keyboard
    from .interactive_ui import build_nav_keyboard

    nav_blocks = build_nav_keyboard(window_id)
    nav_msg_ts = await send_message(
        client,
        channel,
        "Terminal controls:",
        thread_ts=thread_ts,
        blocks=nav_blocks,
    )

    _screenshot_states[skey] = {
        "file_msg_ts": file_msg_ts,
        "nav_msg_ts": nav_msg_ts,
        "window_id": window_id,
    }


async def _cmd_history(
    client: AsyncWebClient,
    user_id: str,
    channel: str,
    thread_ts: str,
    page: int = -1,
) -> None:
    """Show paginated session history."""
    window_id = _resolve_window(user_id, thread_ts)
    if not window_id:
        await send_message(
            client, channel, "No active session in this thread.", thread_ts=thread_ts
        )
        return
    from .history import send_history

    await send_history(client, user_id, channel, thread_ts, window_id, page=page)


async def _cmd_bind(
    client: AsyncWebClient, user_id: str, channel: str, thread_ts: str
) -> None:
    """Open directory browser to bind a session without forwarding any text."""
    # If already bound, inform user
    window_id = _resolve_window(user_id, thread_ts)
    if window_id:
        window = await tmux_manager.find_window_by_id(window_id)
        if window:
            await send_message(
                client,
                channel,
                "This thread already has a session bound.",
                thread_ts=thread_ts,
            )
            return
        # Window dead — unbind so we can rebind
        session_manager.unbind_thread(user_id, thread_ts)

    from .directory_browser import build_directory_browser

    browser = build_directory_browser(user_id)
    resp = await client.chat_postMessage(
        channel=channel,
        text=browser["text"],
        blocks=browser["blocks"],
        thread_ts=thread_ts,
    )
    msg_ts = resp.get("ts", "")
    if msg_ts:
        # No pending_text — session will be created without forwarding a message
        build_directory_browser(user_id, msg_ts=msg_ts, pending_text=None)


async def _cmd_silent(
    client: AsyncWebClient,
    user_id: str,
    channel: str,
    thread_ts: str,
    args: list[str],
) -> None:
    """Toggle silent mode for the bound session."""
    window_id = _resolve_window(user_id, thread_ts)
    if not window_id:
        await send_message(
            client, channel, "No active session in this thread.", thread_ts=thread_ts
        )
        return

    if args and args[0].lower() in ("on", "off"):
        new_silent = args[0].lower() == "on"
    else:
        new_silent = not session_manager.is_silent(window_id)

    session_manager.set_silent(window_id, new_silent)
    status = "ON" if new_silent else "OFF"
    await send_message(
        client, channel, f"🔇 Silent mode: {status}", thread_ts=thread_ts
    )

    # Update thread title with silent/active icon
    from ..bot import _set_thread_title

    await _set_thread_title(client, channel, thread_ts, window_id)


async def _cmd_help(client: AsyncWebClient, channel: str, thread_ts: str) -> None:
    """List all supported commands."""
    help_text = (
        "*Available commands:*\n"
        "• `!bind` — Open directory browser to start a session\n"
        "• `!esc` — Send Escape key to interrupt Claude\n"
        "• `!screenshot` — Capture terminal as PNG\n"
        "• `!history [page]` — Show session message history\n"
        "• `!silent [on|off]` — Toggle silent mode (suppress notifications)\n"
        "• `!unbind` — Kill session and unbind thread\n"
        "• `!help` — Show this help message"
    )
    await send_message(client, channel, help_text, thread_ts=thread_ts)
