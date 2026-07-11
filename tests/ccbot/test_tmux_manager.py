"""Tests for TmuxManager.window_exists — the tri-state liveness check.

window_exists must distinguish a genuinely-absent window (False) from a
transient tmux query failure (None), so callers never unbind a live thread on
a momentary blip.
"""

from unittest.mock import MagicMock, PropertyMock

import pytest
from libtmux.exc import ObjectDoesNotExist

from ccbot.tmux_manager import TmuxManager


class _FakeWindow:
    def __init__(self, window_id: str) -> None:
        self.window_id = window_id


def _mgr_with_server(server) -> TmuxManager:
    m = TmuxManager(session_name="ccbot")
    m._server = server
    return m


@pytest.mark.asyncio
async def test_window_present_returns_true():
    session = MagicMock()
    session.windows = [_FakeWindow("@0"), _FakeWindow("@5")]
    server = MagicMock()
    server.sessions.get.return_value = session

    assert await _mgr_with_server(server).window_exists("@5") is True


@pytest.mark.asyncio
async def test_window_absent_returns_false():
    session = MagicMock()
    session.windows = [_FakeWindow("@0")]
    server = MagicMock()
    server.sessions.get.return_value = session

    assert await _mgr_with_server(server).window_exists("@5") is False


@pytest.mark.asyncio
async def test_session_gone_returns_false():
    """ObjectDoesNotExist = tmux session genuinely absent → window gone."""
    server = MagicMock()
    server.sessions.get.side_effect = ObjectDoesNotExist("session", "ccbot")

    assert await _mgr_with_server(server).window_exists("@5") is False


@pytest.mark.asyncio
async def test_transient_query_error_returns_none():
    """Any other exception = couldn't determine → None (must NOT unbind)."""
    server = MagicMock()
    server.sessions.get.side_effect = RuntimeError("tmux server busy")

    assert await _mgr_with_server(server).window_exists("@5") is None


@pytest.mark.asyncio
async def test_windows_access_error_returns_none():
    """A failure while iterating windows is also inconclusive, not 'gone'."""
    session = MagicMock()
    type(session).windows = PropertyMock(side_effect=RuntimeError("boom"))
    server = MagicMock()
    server.sessions.get.return_value = session

    assert await _mgr_with_server(server).window_exists("@5") is None
