"""Per-window silent filtering at Slack delivery (handle_new_message).

Silent drops the noise for the bound window but keeps the assistant's reply.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ccbot.transports.slack.bot as slackbot
from ccbot.session_monitor import NewMessage


async def _deliver(msg: NewMessage, silent: bool) -> AsyncMock:
    enq = AsyncMock()
    with (
        patch.object(slackbot, "app", MagicMock(client=MagicMock())),
        patch.object(
            slackbot.session_manager,
            "find_users_for_session",
            AsyncMock(return_value=[("U1", "@5", "T1")]),
        ),
        patch.object(slackbot.session_manager, "is_silent", return_value=silent),
        patch.object(
            slackbot.session_manager,
            "resolve_session_for_window",
            AsyncMock(return_value=None),
        ),
        patch.object(slackbot, "_resolve_dm_channel", AsyncMock(return_value="C1")),
        patch.object(slackbot, "record_content_delivery"),
        patch.object(slackbot, "enqueue_content_message", enq),
    ):
        await slackbot.handle_new_message(msg)
    return enq


def _msg(content_type: str, role: str = "assistant") -> NewMessage:
    return NewMessage(
        session_id="s", text="x", is_complete=True, role=role, content_type=content_type
    )


@pytest.mark.asyncio
async def test_silent_drops_noise():
    enq = await _deliver(_msg("thinking"), silent=True)
    enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_silent_keeps_reply():
    enq = await _deliver(_msg("text"), silent=True)
    enq.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_silent_delivers_noise():
    enq = await _deliver(_msg("thinking"), silent=False)
    enq.assert_awaited_once()
