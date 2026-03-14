"""Interactive UI for Claude Code prompts in Slack.

Handles interactive terminal UIs (permission prompts, AskUserQuestion,
ExitPlanMode, etc.) by sending Block Kit messages with navigation buttons.

Action ID format: nav_{key}_{window_id}
Grace period: interactive UI must be absent for CLEAR_GRACE_MISSES consecutive
polls before the interactive message is removed.
"""

import logging
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from ....terminal_parser import extract_interactive_content, is_interactive_ui
from .message_sender import delete_message, edit_message, send_message

logger = logging.getLogger(__name__)

# Tool names that trigger interactive UI via JSONL
INTERACTIVE_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Action ID prefix for navigation buttons
ACTION_NAV_PREFIX = "nav_"

# Grace period: number of consecutive misses before clearing interactive msg
CLEAR_GRACE_MISSES = 3

# (user_id, thread_ts) -> msg_ts
_interactive_msgs: dict[tuple[str, str], str] = {}

# (user_id, thread_ts) -> window_id
_interactive_mode: dict[tuple[str, str], str] = {}

# (user_id, thread_ts) -> consecutive miss count
_grace_counters: dict[tuple[str, str], int] = {}

# (user_id, thread_ts) -> last displayed content (for dedup)
_interactive_last_content: dict[tuple[str, str], str] = {}


def get_interactive_window(user_id: str, thread_ts: str) -> str | None:
    """Get the window_id for user's interactive mode."""
    return _interactive_mode.get((user_id, thread_ts))


def build_nav_keyboard(window_id: str) -> list[dict[str, Any]]:
    """Build Block Kit action blocks with navigation buttons."""

    def _btn(label: str, key: str) -> dict[str, Any]:
        return {
            "type": "button",
            "text": {"type": "plain_text", "text": label},
            "action_id": f"{ACTION_NAV_PREFIX}{key}_{window_id}",
        }

    return [
        {
            "type": "actions",
            "elements": [
                _btn("\u2423 Space", "space"),
                _btn("\u2191", "up"),
                _btn("\u21e5 Tab", "tab"),
            ],
        },
        {
            "type": "actions",
            "elements": [
                _btn("\u2190", "left"),
                _btn("\u2193", "down"),
                _btn("\u2192", "right"),
            ],
        },
        {
            "type": "actions",
            "elements": [
                _btn("\u238b Esc", "esc"),
                _btn("\U0001f504 Refresh", "refresh"),
                _btn("\u23ce Enter", "enter"),
            ],
        },
    ]


async def handle_interactive_ui(
    client: AsyncWebClient,
    user_id: str,
    thread_ts: str,
    window_id: str,
    channel: str,
    pane_text: str,
) -> bool:
    """Send or update interactive UI message. Returns True if UI was detected."""
    if not is_interactive_ui(pane_text):
        return False

    content = extract_interactive_content(pane_text)
    if not content:
        return False

    ikey = (user_id, thread_ts)
    # Reset grace counter since UI is present
    _grace_counters.pop(ikey, None)

    nav_blocks = build_nav_keyboard(window_id)
    text_block: dict[str, Any] = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"```\n{content.content}\n```"},
    }
    blocks = [text_block, *nav_blocks]

    existing_ts = _interactive_msgs.get(ikey)
    if existing_ts:
        # Dedup: skip edit if content hasn't changed
        if _interactive_last_content.get(ikey) == content.content:
            return True
        ok = await edit_message(
            client, channel, existing_ts, content.content, blocks=blocks
        )
        if ok:
            _interactive_mode[ikey] = window_id
            _interactive_last_content[ikey] = content.content
            return True
        # Edit failed — send new message
        _interactive_msgs.pop(ikey, None)
        _interactive_last_content.pop(ikey, None)

    ts = await send_message(
        client, channel, content.content, thread_ts=thread_ts, blocks=blocks
    )
    if ts:
        _interactive_msgs[ikey] = ts
        _interactive_mode[ikey] = window_id
        _interactive_last_content[ikey] = content.content
        return True
    return False


async def clear_interactive_msg(
    client: AsyncWebClient,
    user_id: str,
    thread_ts: str,
    channel: str,
) -> None:
    """Clear tracked interactive message and exit interactive mode."""
    ikey = (user_id, thread_ts)
    msg_ts = _interactive_msgs.pop(ikey, None)
    _interactive_mode.pop(ikey, None)
    _grace_counters.pop(ikey, None)
    _interactive_last_content.pop(ikey, None)
    if msg_ts:
        await delete_message(client, channel, msg_ts)
