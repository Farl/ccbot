"""Unified command dispatch for Slack transport.

Handles both text prefix commands (!esc) and Slack slash commands (/esc).
Both call dispatch_command() — the same logic handles both.

Supported commands:
  esc        — Send Escape key to the bound tmux window
  unbind     — Kill window and unbind thread
  screenshot — Capture terminal as PNG and upload to Slack
  history    — Show paginated session history

Import depth from this file (src/ccbot/transports/slack/handlers/):
  .... = 4 levels up = src/ccbot/
"""

import logging

from slack_sdk.web.async_client import AsyncWebClient

from ....session import session_manager
from ....tmux_manager import tmux_manager
from .message_sender import send_message

logger = logging.getLogger(__name__)


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
        await send_message(client, channel, "No active session in this thread.", thread_ts=thread_ts)
        return
    await tmux_manager.send_keys(window_id, "Escape", enter=False, literal=False)
    await send_message(client, channel, "↩ Escape sent.", thread_ts=thread_ts)


async def _cmd_unbind(
    client: AsyncWebClient, user_id: str, channel: str, thread_ts: str
) -> None:
    """Kill window and unbind thread."""
    window_id = _resolve_window(user_id, thread_ts)
    if not window_id:
        await send_message(client, channel, "No active session in this thread.", thread_ts=thread_ts)
        return
    from .cleanup import cleanup_thread
    await cleanup_thread(user_id, thread_ts, client, channel, kill_window=True)
    await send_message(client, channel, "Session unbound.", thread_ts=thread_ts)


async def _cmd_screenshot(
    client: AsyncWebClient, user_id: str, channel: str, thread_ts: str
) -> None:
    """Capture terminal as PNG and upload to Slack."""
    window_id = _resolve_window(user_id, thread_ts)
    if not window_id:
        await send_message(client, channel, "No active session in this thread.", thread_ts=thread_ts)
        return

    pane_text = await tmux_manager.capture_pane(window_id)
    if not pane_text:
        await send_message(client, channel, "Failed to capture terminal.", thread_ts=thread_ts)
        return

    try:
        from ....screenshot import text_to_image  # 4 dots = src/ccbot/
        png_bytes = await text_to_image(pane_text)
    except Exception as e:
        logger.error("Screenshot render failed: %s", e)
        await send_message(client, channel, f"Screenshot failed: {e}", thread_ts=thread_ts)
        return

    try:
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
    except Exception as e:
        logger.error("Screenshot upload failed: %s", e)
        await send_message(client, channel, f"Upload failed: {e}", thread_ts=thread_ts)


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
        await send_message(client, channel, "No active session in this thread.", thread_ts=thread_ts)
        return
    from .history import send_history  # type: ignore[import]
    await send_history(client, user_id, channel, thread_ts, window_id, page=page)
