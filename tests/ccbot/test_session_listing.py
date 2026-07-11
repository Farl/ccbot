"""Tests for list_sessions_for_directory: SDK detection, running-first ordering.

Covers the picker's data layer:
  - `claude -p` / SDK sessions are flagged (is_sdk) but never dropped.
  - Sessions live in a tmux window are marked is_running, sorted first, and
    surfaced even when recent -p runs would push them past the mtime cap.
"""

import json
import os
from pathlib import Path

import pytest

from ccbot.session import SessionManager


def _write_session(
    project_dir: Path,
    session_id: str,
    *,
    sdk: bool,
    mtime: float,
    text: str = "hi",
    entrypoint: str | None = None,
    prompt_source: str | None = None,
) -> None:
    """Write a minimal one-turn session JSONL with an entrypoint marker."""
    entry = {
        "type": "user",
        "message": {"role": "user", "content": text},
        "entrypoint": entrypoint or ("sdk-cli" if sdk else "cli"),
        "promptSource": prompt_source or ("sdk" if sdk else "typed"),
        "sessionId": session_id,
    }
    path = project_dir / f"{session_id}.jsonl"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _write_empty_session(project_dir: Path, session_id: str, *, mtime: float) -> None:
    """Write a session file with no parseable messages (message_count == 0)."""
    path = project_dir / f"{session_id}.jsonl"
    path.write_text('{"type": "summary", "summary": ""}\n', encoding="utf-8")
    os.utime(path, (mtime, mtime))


@pytest.fixture
def mgr(monkeypatch, tmp_path) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    m = SessionManager()
    # Point the projects path at a temp dir and stub the tmux liveness check.
    import ccbot.session as session_mod

    monkeypatch.setattr(session_mod.config, "claude_projects_path", tmp_path)
    return m


def _project_dir(mgr: SessionManager, tmp_path: Path, cwd: str) -> Path:
    d = tmp_path / mgr._encode_cwd(cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d


class _FakeWindow:
    def __init__(self, window_id: str) -> None:
        self.window_id = window_id


@pytest.mark.asyncio
async def test_sdk_sessions_flagged_but_not_dropped(monkeypatch, mgr, tmp_path):
    cwd = "/Users/x/proj"
    pdir = _project_dir(mgr, tmp_path, cwd)
    _write_session(pdir, "aaaaaaaa-1", sdk=False, mtime=200)
    _write_session(pdir, "bbbbbbbb-2", sdk=True, mtime=100)
    monkeypatch.setattr(mgr, "_running_session_ids_for_cwd", _async_return(set()))

    sessions = await mgr.list_sessions_for_directory(cwd)
    by_id = {s.session_id: s for s in sessions}

    assert len(sessions) == 2  # SDK session kept, not filtered
    assert by_id["aaaaaaaa-1"].is_sdk is False
    assert by_id["bbbbbbbb-2"].is_sdk is True


@pytest.mark.asyncio
async def test_claude_vscode_not_flagged_sdk(monkeypatch, mgr, tmp_path):
    """Interactive claude-vscode sessions carry promptSource='sdk' but are NOT
    headless -p — they must not be tagged is_sdk (regression)."""
    cwd = "/Users/x/proj"
    pdir = _project_dir(mgr, tmp_path, cwd)
    _write_session(
        pdir,
        "vscode00-1",
        sdk=False,
        mtime=100,
        entrypoint="claude-vscode",
        prompt_source="sdk",
    )
    monkeypatch.setattr(mgr, "_running_session_ids_for_cwd", _async_return(set()))

    sessions = await mgr.list_sessions_for_directory(cwd)

    assert sessions[0].is_sdk is False


@pytest.mark.asyncio
async def test_running_empty_session_still_listed(monkeypatch, mgr, tmp_path):
    """A live session whose transcript has no messages yet is still surfaced."""
    cwd = "/Users/x/proj"
    pdir = _project_dir(mgr, tmp_path, cwd)
    _write_empty_session(pdir, "empty-run", mtime=10)
    mgr.window_states["@3"] = _window_state("empty-run", cwd)
    monkeypatch.setattr(
        "ccbot.session.tmux_manager.list_windows", _async_return([_FakeWindow("@3")])
    )

    sessions = await mgr.list_sessions_for_directory(cwd)

    assert [s.session_id for s in sessions] == ["empty-run"]
    assert sessions[0].is_running is True


@pytest.mark.asyncio
async def test_running_session_first_even_when_oldest(monkeypatch, mgr, tmp_path):
    """An idle interactive session buried under many recent -p runs still shows,
    marked running, at the top."""
    cwd = "/Users/x/proj"
    pdir = _project_dir(mgr, tmp_path, cwd)
    running_id = "11111111-run"
    _write_session(pdir, running_id, sdk=False, mtime=1)  # oldest
    for i in range(15):  # 15 newer -p runs would otherwise bury it (cap 10)
        _write_session(pdir, f"pp{i:06d}-sdk", sdk=True, mtime=100 + i)

    # running_id is live in window @9 for this cwd
    mgr.window_states["@9"] = _window_state(running_id, cwd)
    monkeypatch.setattr(
        "ccbot.session.tmux_manager.list_windows", _async_return([_FakeWindow("@9")])
    )

    sessions = await mgr.list_sessions_for_directory(cwd)

    assert sessions[0].session_id == running_id
    assert sessions[0].is_running is True
    # Non-running tail capped at 10, but the running one is extra on top.
    assert len(sessions) == 11
    assert all(not s.is_running for s in sessions[1:])


@pytest.mark.asyncio
async def test_closed_session_not_marked_running(monkeypatch, mgr, tmp_path):
    """A session in window_states whose window is no longer live isn't running."""
    cwd = "/Users/x/proj"
    pdir = _project_dir(mgr, tmp_path, cwd)
    _write_session(pdir, "cccccccc-3", sdk=False, mtime=50)
    mgr.window_states["@2"] = _window_state("cccccccc-3", cwd)
    # No live windows returned → nothing counts as running.
    monkeypatch.setattr("ccbot.session.tmux_manager.list_windows", _async_return([]))

    sessions = await mgr.list_sessions_for_directory(cwd)

    assert sessions[0].is_running is False


@pytest.mark.asyncio
async def test_window_id_for_session_resolves_live_window(monkeypatch, mgr):
    mgr.window_states["@4"] = _window_state("sess-xyz", "/Users/x/proj")
    monkeypatch.setattr(
        "ccbot.session.tmux_manager.list_windows", _async_return([_FakeWindow("@4")])
    )
    assert await mgr.window_id_for_session("sess-xyz") == "@4"


@pytest.mark.asyncio
async def test_window_id_for_session_none_when_not_live(monkeypatch, mgr):
    mgr.window_states["@4"] = _window_state("sess-xyz", "/Users/x/proj")
    monkeypatch.setattr("ccbot.session.tmux_manager.list_windows", _async_return([]))
    assert await mgr.window_id_for_session("sess-xyz") is None


def _window_state(session_id: str, cwd: str):
    from ccbot.session import WindowState

    return WindowState(session_id=session_id, cwd=cwd, window_name="w")


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner
