"""Slack message sending helpers wrapping AsyncWebClient.

Provides send, edit, delete, and long-message (auto-split) operations.
All functions log errors and return success indicators instead of raising.
"""

import logging
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from ..formatter import to_blocks, to_mrkdwn
from ..splitter import split_message

logger = logging.getLogger(__name__)


async def send_message(
    client: AsyncWebClient,
    channel: str,
    text: str,
    thread_ts: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
) -> str | None:
    """Send a message to a Slack channel. Returns ts on success, None on failure."""
    try:
        kwargs: dict[str, Any] = {
            "channel": channel,
            "text": to_mrkdwn(text),
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if blocks:
            kwargs["blocks"] = blocks
        elif (table_blocks := to_blocks(text)) is not None:
            kwargs["blocks"] = table_blocks
        resp = await client.chat_postMessage(**kwargs)
        return resp.get("ts")
    except Exception as e:
        logger.error("Failed to send to %s: %s", channel, e)
        return None


async def edit_message(
    client: AsyncWebClient,
    channel: str,
    ts: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
) -> bool:
    """Edit an existing message. Returns True on success."""
    try:
        kwargs: dict[str, Any] = {
            "channel": channel,
            "ts": ts,
            "text": to_mrkdwn(text),
        }
        if blocks:
            kwargs["blocks"] = blocks
        elif (table_blocks := to_blocks(text)) is not None:
            kwargs["blocks"] = table_blocks
        await client.chat_update(**kwargs)
        return True
    except Exception as e:
        logger.error("Failed to edit %s: %s", ts, e)
        return False


async def delete_message(
    client: AsyncWebClient,
    channel: str,
    ts: str,
) -> bool:
    """Delete a message. Returns True on success."""
    try:
        await client.chat_delete(channel=channel, ts=ts)
        return True
    except Exception as e:
        logger.error("Failed to delete %s: %s", ts, e)
        return False


async def send_long_message(
    client: AsyncWebClient,
    channel: str,
    text: str,
    thread_ts: str | None = None,
) -> str | None:
    """Split and send a long message. Returns first message ts."""
    parts = split_message(text)
    first_ts = None
    for part in parts:
        ts = await send_message(client, channel, part, thread_ts=thread_ts)
        if first_ts is None:
            first_ts = ts
    return first_ts
