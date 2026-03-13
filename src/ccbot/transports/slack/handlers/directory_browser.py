"""Directory browser with Block Kit for Slack transport.

Provides a directory navigation UI using Slack's interactive blocks:
  - Navigate directories with button clicks
  - Pagination for large directory listings
  - Create tmux sessions from selected directories

Action ID constants are exported for pattern matching in the bot module.
"""

import logging
from pathlib import Path
from typing import Any

from ....config import config
from ....session import session_manager
from ....tmux_manager import tmux_manager

logger = logging.getLogger(__name__)

DIRS_PER_PAGE = 8

# Action ID prefixes/constants
ACTION_DIR_SELECT = "dir_select_"
ACTION_DIR_UP = "dir_up"
ACTION_DIR_CONFIRM = "dir_confirm"
ACTION_DIR_CANCEL = "dir_cancel"
ACTION_DIR_PAGE = "dir_page_"

# Per-message browse state: msg_ts -> {path, dirs, page, user_id}
_browse_states: dict[str, dict[str, Any]] = {}


def get_browse_state(user_id: str, msg_ts: str | None = None) -> dict[str, Any] | None:
    """Get current browse state for a user.

    If msg_ts is given, look up by message ts (preferred).
    Otherwise fall back to scanning for user_id (legacy).
    """
    if msg_ts and msg_ts in _browse_states:
        state = _browse_states[msg_ts]
        if state.get("user_id") == user_id:
            return state
    # Fallback: find by user_id
    for _ts, state in _browse_states.items():
        if state.get("user_id") == user_id:
            return state
    return None


def clear_browse_state(user_id: str, msg_ts: str | None = None) -> None:
    """Clear browse state for a user."""
    if msg_ts and msg_ts in _browse_states:
        _browse_states.pop(msg_ts, None)
        return
    # Fallback: clear by user_id
    to_remove = [ts for ts, s in _browse_states.items() if s.get("user_id") == user_id]
    for ts in to_remove:
        _browse_states.pop(ts, None)


def build_directory_browser(
    user_id: str,
    path: Path | None = None,
    page: int = 0,
    msg_ts: str | None = None,
) -> dict[str, Any]:
    """Build directory browser UI with Block Kit blocks.

    Returns dict with 'text' (fallback) and 'blocks' keys.
    If msg_ts is provided, state is keyed by message ts for reliable lookup.
    """
    if path is None:
        path = Path.home()
    path = path.expanduser().resolve()
    if not path.exists() or not path.is_dir():
        path = Path.home()

    try:
        subdirs = sorted(
            d.name
            for d in path.iterdir()
            if d.is_dir() and (config.show_hidden_dirs or not d.name.startswith("."))
        )
    except (PermissionError, OSError):
        subdirs = []

    total_pages = max(1, (len(subdirs) + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DIRS_PER_PAGE
    page_dirs = subdirs[start : start + DIRS_PER_PAGE]

    # Save state keyed by msg_ts (if available) for reliable callback lookup
    state = {
        "path": str(path),
        "dirs": subdirs,
        "page": page,
        "user_id": user_id,
    }
    key = msg_ts if msg_ts else user_id
    _browse_states[key] = state

    display_path = str(path).replace(str(Path.home()), "~")
    header_text = f"*Select Working Directory*\n`{display_path}`"

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header_text},
        }
    ]

    # Directory buttons (2 per row)
    if page_dirs:
        for i in range(0, len(page_dirs), 2):
            elements: list[dict[str, Any]] = []
            for j in range(min(2, len(page_dirs) - i)):
                name = page_dirs[i + j]
                idx = start + i + j
                display = name[:20] + "..." if len(name) > 20 else name
                elements.append(
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": f"\U0001f4c1 {display}"},
                        "action_id": f"{ACTION_DIR_SELECT}{idx}",
                    }
                )
            blocks.append({"type": "actions", "elements": elements})
    else:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_(No subdirectories)_"},
            }
        )

    # Pagination row
    if total_pages > 1:
        nav_elements: list[dict[str, Any]] = []
        if page > 0:
            nav_elements.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\u25c0 Prev"},
                    "action_id": f"{ACTION_DIR_PAGE}{page - 1}",
                }
            )
        nav_elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": f"{page + 1}/{total_pages}"},
                "action_id": "noop",
            }
        )
        if page < total_pages - 1:
            nav_elements.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Next \u25b6"},
                    "action_id": f"{ACTION_DIR_PAGE}{page + 1}",
                }
            )
        blocks.append({"type": "actions", "elements": nav_elements})

    # Action row: Up / Select / Cancel
    action_elements: list[dict[str, Any]] = []
    if path != path.parent:
        action_elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ".."},
                "action_id": ACTION_DIR_UP,
            }
        )
    action_elements.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "\u2705 Select"},
            "action_id": ACTION_DIR_CONFIRM,
            "style": "primary",
        }
    )
    action_elements.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Cancel"},
            "action_id": ACTION_DIR_CANCEL,
            "style": "danger",
        }
    )
    blocks.append({"type": "actions", "elements": action_elements})

    return {
        "text": f"Select directory: {display_path}",
        "blocks": blocks,
    }


async def create_session_for_thread(
    user_id: str,
    thread_ts: str,
    directory: str,
) -> str | None:
    """Create a tmux window and bind it to a Slack thread.

    Returns the window_id on success, None on failure.
    """
    success, msg, window_name, window_id = await tmux_manager.create_window(directory)
    if not success:
        logger.error("Failed to create window for %s: %s", directory, msg)
        return None

    session_manager.bind_thread(user_id, thread_ts, window_id, window_name)

    # Wait for session_map entry (hook fires on SessionStart)
    await session_manager.wait_for_session_map_entry(window_id, timeout=10.0)

    logger.info(
        "Created session: user=%s, thread=%s, window=%s (%s)",
        user_id,
        thread_ts,
        window_id,
        window_name,
    )
    return window_id
