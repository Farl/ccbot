"""State cleanup for Slack transport.

Handles cleanup when a thread is unbound: kills the tmux window,
clears interactive UI messages, and removes status messages.
"""

import logging

from slack_sdk.web.async_client import AsyncWebClient

from ....session import session_manager
from ....tmux_manager import tmux_manager
from .interactive_ui import clear_interactive_msg
from .status_polling import clear_status

logger = logging.getLogger(__name__)


async def cleanup_thread(
    user_id: str,
    thread_ts: str,
    client: AsyncWebClient,
    channel: str,
    kill_window: bool = True,
) -> None:
    """Unbind a thread and clean up all associated state.

    Removes interactive UI messages, status messages, and optionally
    kills the tmux window.
    """
    window_id = session_manager.unbind_thread(user_id, thread_ts)
    if not window_id:
        return
    await clear_interactive_msg(client, user_id, thread_ts, channel)
    await clear_status(user_id, thread_ts, client, channel)
    if kill_window:
        await tmux_manager.kill_window(window_id)
    logger.info(
        "Cleaned up thread: user=%s, thread=%s, window=%s, killed=%s",
        user_id,
        thread_ts,
        window_id,
        kill_window,
    )
