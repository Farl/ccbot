"""Paginated session history for Slack transport.

Renders JSONL transcript as paginated Block Kit messages with prev/next buttons.

Action ID format: hist_{direction}_{page}_{window_id}
  direction: "prev" or "next"
  page: current displayed page (0-based)
  window_id: e.g. "@5"
Parse by splitting on "_" with limit 3 → ["hist", direction, page, window_id].
"""

import logging
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from ....config import config
from ....session import session_manager
from ....transcript_parser import TranscriptParser
from ..splitter import split_message
from .message_sender import edit_message, send_message

logger = logging.getLogger(__name__)

HISTORY_PAGE_SIZE = 4000  # chars per Slack page (conservative under 4000 limit)


def build_history_nav_blocks(
    window_id: str, page: int, total_pages: int
) -> list[dict[str, Any]]:
    """Build Block Kit prev/next navigation buttons.

    Returns empty list if only one page (no navigation needed).
    """
    if total_pages <= 1:
        return []

    elements: list[dict[str, Any]] = []
    if page > 0:
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "◀ Older"},
                "action_id": f"hist_prev_{page}_{window_id}",
            }
        )
    elements.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": f"{page + 1}/{total_pages}"},
            "action_id": "noop",
        }
    )
    if page < total_pages - 1:
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Newer ▶"},
                "action_id": f"hist_next_{page}_{window_id}",
            }
        )
    return [{"type": "actions", "elements": elements}]


def parse_history_action_id(action_id: str) -> tuple[int, str]:
    """Parse a hist_ action_id → (target_page, window_id).

    Format: hist_{prev|next}_{current_page}_{window_id}
    prev → target = current_page - 1
    next → target = current_page + 1
    """
    parts = action_id.split("_", 3)  # ["hist", direction, page, window_id]
    direction = parts[1]
    current_page = int(parts[2])
    window_id = parts[3]
    return (current_page - 1 if direction == "prev" else current_page + 1), window_id


async def send_history(
    client: AsyncWebClient,
    user_id: str,
    channel: str,
    thread_ts: str,
    window_id: str,
    page: int = -1,
    edit_ts: str | None = None,
) -> None:
    """Send (or edit) paginated history for a session.

    page=-1 defaults to last page (newest messages).
    edit_ts: if set, edit this Slack message instead of sending new.
    """
    display_name = session_manager.get_display_name(window_id)
    messages, total = await session_manager.get_recent_messages(window_id)

    if total == 0 or not messages:
        text = f"📋 [{display_name}] No messages yet."
        if edit_ts:
            await edit_message(client, channel, edit_ts, text, blocks=[])
        else:
            await send_message(client, channel, text, thread_ts=thread_ts)
        return

    if not config.show_user_messages:
        messages = [m for m in messages if m["role"] == "assistant"]

    _start = TranscriptParser.EXPANDABLE_QUOTE_START
    _end = TranscriptParser.EXPANDABLE_QUOTE_END

    header = f"📋 [{display_name}] {len(messages)} messages"
    lines = [header]
    for msg in messages:
        ts = msg.get("timestamp", "")
        hh_mm = ""
        if ts:
            try:
                time_part = ts.split("T")[1] if "T" in ts else ts
                hh_mm = time_part[:5]
            except (IndexError, TypeError):
                pass

        lines.append(f"───── {hh_mm} ─────" if hh_mm else "─────────────")

        msg_text = msg["text"].replace(_start, "").replace(_end, "")
        content_type = msg.get("content_type", "text")
        if msg.get("role") == "user":
            lines.append(f"👤 {msg_text}")
        elif content_type == "thinking":
            lines.append(f"∴ Thinking…\n{msg_text}")
        else:
            lines.append(msg_text)

    full_text = "\n\n".join(lines)
    pages = split_message(full_text, max_length=HISTORY_PAGE_SIZE)

    if page < 0:
        page = len(pages) - 1
    page = max(0, min(page, len(pages) - 1))

    text = pages[page]
    nav_blocks = build_history_nav_blocks(window_id, page, len(pages))

    if edit_ts:
        await edit_message(client, channel, edit_ts, text, blocks=nav_blocks or None)
    else:
        await send_message(
            client, channel, text, thread_ts=thread_ts, blocks=nav_blocks or None
        )
