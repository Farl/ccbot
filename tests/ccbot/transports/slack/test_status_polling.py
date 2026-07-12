"""Tests for Slack status_polling thread-status syncing.

Focus: `_sync_thread_status` must re-assert a non-empty "thinking" status on
every call, because Slack auto-clears the assistant thread status whenever the
app posts a message to the thread (and after a 2-minute idle timeout). Caching
"we already set this text" would leave the indicator blank after the first
content/tool/status message clears it. The empty (clear) case is still deduped
so idle threads don't spam setStatus("").
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ccbot.transports.slack.handlers.status_polling as sp


@pytest.fixture
def _reset_status_state():
    """Reset module-level dedup state and callback before/after each test."""
    sp._last_thread_status.clear()
    sp._status_clear_grace.clear()
    saved = sp._set_thread_status
    yield
    sp._last_thread_status.clear()
    sp._status_clear_grace.clear()
    sp._set_thread_status = saved


@pytest.mark.usefixtures("_reset_status_state")
class TestSyncThreadStatus:
    @pytest.mark.asyncio
    async def test_nonempty_status_reasserted_on_repeat(self):
        """Same non-empty status twice → setStatus called BOTH times.

        Slack auto-clears the status when the app posts a message, so a repeat
        with identical text is the poller re-asserting a status that may have
        been cleared. It must reach the API, not be deduped away.
        """
        cb = AsyncMock()
        sp._set_thread_status = cb

        await sp._sync_thread_status("U1", "ts1", "C1", "Thinking…")
        await sp._sync_thread_status("U1", "ts1", "C1", "Thinking…")

        assert cb.await_count == 2
        cb.assert_awaited_with("C1", "ts1", "Thinking…")

    @pytest.mark.asyncio
    async def test_empty_status_deduped(self):
        """Repeated empty (clear) status → setStatus called only once."""
        cb = AsyncMock()
        sp._set_thread_status = cb

        await sp._sync_thread_status("U1", "ts1", "C1", "")
        await sp._sync_thread_status("U1", "ts1", "C1", "")

        assert cb.await_count == 1
        cb.assert_awaited_with("C1", "ts1", "")

    @pytest.mark.asyncio
    async def test_clear_after_active_status_sends_empty(self):
        """Active status then clear → both reach the API (no false dedup)."""
        cb = AsyncMock()
        sp._set_thread_status = cb

        await sp._sync_thread_status("U1", "ts1", "C1", "Reading file")
        await sp._sync_thread_status("U1", "ts1", "C1", "")

        assert cb.await_count == 2
        cb.assert_awaited_with("C1", "ts1", "")


@pytest.mark.usefixtures("_reset_status_state")
class TestStatusShownAsNativeOnly:
    """The working status is the native thread status — never a chat message."""

    @pytest.mark.asyncio
    async def test_thinking_pane_sets_native_status_no_message(self):
        """A thinking pane → native setStatus with the status text, no chat post."""
        cb = AsyncMock()
        sp._set_thread_status = cb
        window = MagicMock()
        window.window_id = "@7"
        pane = (
            "some output\n"
            "✻ Ideating… (12s)\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  ⏵⏵ auto mode on · esc to interrupt · ← for agents\n"
        )
        with (
            patch.object(sp, "tmux_manager") as mock_tmux,
            patch.object(sp.config, "show_status", True),
            patch.object(sp.session_manager, "is_silent", return_value=False),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane)

            await sp.update_status_for_window(AsyncMock(), "U1", "ts1", "@7", "C1")

        cb.assert_awaited_once_with("C1", "ts1", "Ideating… (12s)")


_IDLE_PANE = (
    "some output\n"
    "● Tool result\n"
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
    "  ⏵⏵ auto mode on · ← for agents\n"  # NO "esc to interrupt" → idle
)

_ACTIVE_PANE = (
    "some output\n"
    "✻ Ideating… (3s)\n"
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
    "  ⏵⏵ auto mode on · esc to interrupt · ← for agents\n"
)


@pytest.mark.usefixtures("_reset_status_state")
class TestStatusClearGrace:
    """An idle pane holds the native status for a grace window before clearing,
    so the indicator doesn't blink out before the (slower) trailing reply lands.
    """

    @staticmethod
    def _mock_tmux(mock_tmux, pane: str):
        window = MagicMock()
        window.window_id = "@7"
        mock_tmux.find_window_by_id = AsyncMock(return_value=window)
        mock_tmux.capture_pane = AsyncMock(return_value=pane)

    @pytest.mark.asyncio
    async def test_idle_holds_then_clears_after_grace(self):
        """Idle polls below the grace threshold don't clear; the poll that
        crosses it does."""
        cb = AsyncMock()
        sp._set_thread_status = cb
        with (
            patch.object(sp, "tmux_manager") as mock_tmux,
            patch.object(sp.config, "show_status", True),
        ):
            self._mock_tmux(mock_tmux, _IDLE_PANE)
            for _ in range(sp.STATUS_CLEAR_GRACE_POLLS - 1):
                await sp.update_status_for_window(AsyncMock(), "U1", "ts1", "@7", "C1")
            cb.assert_not_awaited()  # still held — indicator stays

            await sp.update_status_for_window(AsyncMock(), "U1", "ts1", "@7", "C1")
            cb.assert_awaited_once_with("C1", "ts1", "")  # grace elapsed → cleared

    @pytest.mark.asyncio
    async def test_active_status_resets_grace(self):
        """An active poll mid-grace resets the counter, so a later idle run must
        wait out the full grace again."""
        cb = AsyncMock()
        sp._set_thread_status = cb
        with (
            patch.object(sp, "tmux_manager") as mock_tmux,
            patch.object(sp.config, "show_status", True),
        ):
            self._mock_tmux(mock_tmux, _IDLE_PANE)
            for _ in range(sp.STATUS_CLEAR_GRACE_POLLS - 1):
                await sp.update_status_for_window(AsyncMock(), "U1", "ts1", "@7", "C1")

            # Active again → grace reset.
            mock_tmux.capture_pane = AsyncMock(return_value=_ACTIVE_PANE)
            await sp.update_status_for_window(AsyncMock(), "U1", "ts1", "@7", "C1")
            assert ("U1", "ts1") not in sp._status_clear_grace

            # Idle again: grace-1 polls must NOT clear (counter started over).
            cb.reset_mock()
            mock_tmux.capture_pane = AsyncMock(return_value=_IDLE_PANE)
            for _ in range(sp.STATUS_CLEAR_GRACE_POLLS - 1):
                await sp.update_status_for_window(AsyncMock(), "U1", "ts1", "@7", "C1")
            assert ("C1", "ts1", "") not in [c.args for c in cb.await_args_list]
