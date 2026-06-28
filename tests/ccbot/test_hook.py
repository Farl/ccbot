"""Tests for Claude Code session tracking hook."""

import io
import json
import sys

import pytest

import ccbot.hook as hook_mod
from ccbot.hook import (
    _UUID_RE,
    _claude_cmdline_is_headless,
    _executable_basename,
    _find_existing_hook,
    _find_nearest_claude_cmdline,
    _is_claude_process,
    _is_noninteractive_invocation,
    hook_main,
)


class TestUuidRegex:
    @pytest.mark.parametrize(
        "value",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "00000000-0000-0000-0000-000000000000",
            "abcdef01-2345-6789-abcd-ef0123456789",
        ],
        ids=["standard", "all-zeros", "all-hex"],
    )
    def test_valid_uuid_matches(self, value: str) -> None:
        assert _UUID_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-uuid",
            "550e8400-e29b-41d4-a716",
            "550e8400-e29b-41d4-a716-44665544000g",
            "",
        ],
        ids=["gibberish", "truncated", "invalid-hex-char", "empty"],
    )
    def test_invalid_uuid_no_match(self, value: str) -> None:
        assert _UUID_RE.match(value) is None


class TestFindExistingHook:
    def test_hook_present(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "ccbot hook", "timeout": 5}
                        ]
                    }
                ]
            }
        }
        result = _find_existing_hook(settings)
        assert result is not None
        _entry, hook_dict = result
        assert hook_dict["command"] == "ccbot hook"

    def test_no_hooks_key(self) -> None:
        assert _find_existing_hook({}) is None

    def test_different_hook_command(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "other-tool hook"}]}
                ]
            }
        }
        assert _find_existing_hook(settings) is None

    def test_full_path_matches(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/bin/ccbot hook",
                                "timeout": 5,
                            }
                        ]
                    }
                ]
            }
        }
        result = _find_existing_hook(settings)
        assert result is not None
        _entry, hook_dict = result
        assert hook_dict["command"] == "/usr/bin/ccbot hook"


class TestHookMainValidation:
    def _run_hook_main(
        self, monkeypatch: pytest.MonkeyPatch, payload: dict, *, tmux_pane: str = ""
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["ccbot", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        if tmux_pane:
            monkeypatch.setenv("TMUX_PANE", tmux_pane)
        else:
            monkeypatch.delenv("TMUX_PANE", raising=False)
        hook_main()

    def test_missing_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {"cwd": "/tmp", "hook_event_name": "SessionStart"},
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_invalid_uuid_format(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "not-a-uuid",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_relative_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "relative/path",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_non_session_start_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "Stop",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_headless_invocation_not_registered(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """A headless `claude -p` (entrypoint=sdk-cli) must not write session_map,
        even when it shares an interactive session's TMUX_PANE."""
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
            tmux_pane="%1",
        )
        assert not (tmp_path / "session_map.json").exists()


class TestExecutableBasename:
    @pytest.mark.parametrize(
        ("cmdline", "expected"),
        [
            ("claude", "claude"),
            ("claude -p hi", "claude"),
            ("/Users/x/.local/bin/claude --resume abc", "claude"),
            ("env -u CLAUDECODE claude", "claude"),
            ("env FOO=bar -u CLAUDECODE claude -p hi", "claude"),
            ("/bin/zsh -c 'eval ... claude ...'", "zsh"),
            ("", ""),
        ],
        ids=[
            "plain",
            "with-flag",
            "abs-path",
            "env-wrapped",
            "env-assign-and-opt",
            "shell-mentioning-claude",
            "empty",
        ],
    )
    def test_basename(self, cmdline: str, expected: str) -> None:
        assert _executable_basename(cmdline) == expected


class TestIsClaudeProcess:
    @pytest.mark.parametrize(
        "cmdline",
        [
            "claude",
            "claude -p hi",
            "/opt/native-binary/claude --output-format stream-json",
            "env -u CLAUDECODE claude",
        ],
    )
    def test_true(self, cmdline: str) -> None:
        assert _is_claude_process(cmdline) is True

    @pytest.mark.parametrize(
        "cmdline",
        [
            "/bin/zsh -c source ... && eval 'claude -p hi'",  # shell only mentions claude
            "-zsh",
            "login -pf farllee",
            "python -m pytest",
        ],
    )
    def test_false(self, cmdline: str) -> None:
        assert _is_claude_process(cmdline) is False


class TestClaudeCmdlineIsHeadless:
    @pytest.mark.parametrize(
        "cmdline",
        ["claude -p hi", "claude --print x", "claude --bg", "claude --remote-control"],
    )
    def test_headless(self, cmdline: str) -> None:
        assert _claude_cmdline_is_headless(cmdline) is True

    @pytest.mark.parametrize(
        "cmdline",
        ["claude", "claude --resume abc123", "env -u CLAUDECODE claude"],
    )
    def test_interactive(self, cmdline: str) -> None:
        assert _claude_cmdline_is_headless(cmdline) is False


class TestFindNearestClaudeCmdline:
    def test_returns_nearest_claude_skipping_shell(self) -> None:
        # hook(parent=100) -> claude -p (100) -> zsh (99) -> claude interactive (98)
        parents = {100: 99, 99: 98, 98: 1}
        cmds = {
            100: "claude -p hi",
            99: "/bin/zsh -c '... claude ...'",
            98: "claude",
        }
        result = _find_nearest_claude_cmdline(100, parents.get, cmds.get)
        assert result == "claude -p hi"

    def test_walks_through_shell_to_claude(self) -> None:
        # hook(parent=50=sh) -> claude interactive (49)
        parents = {50: 49, 49: 1}
        cmds = {50: "sh -c ccbot hook", 49: "claude"}
        assert _find_nearest_claude_cmdline(50, parents.get, cmds.get) == "claude"

    def test_no_claude_ancestor_returns_none(self) -> None:
        parents = {10: 9, 9: 1}
        cmds = {10: "-zsh", 9: "login -pf user"}
        assert _find_nearest_claude_cmdline(10, parents.get, cmds.get) is None

    def test_stops_at_max_depth(self) -> None:
        # Long chain of shells with claude beyond the depth limit — must not find it.
        parents = {n: n - 1 for n in range(2, 30)}
        cmds = {n: "sh" for n in range(2, 30)}
        cmds[2] = "claude"
        assert (
            _find_nearest_claude_cmdline(29, parents.get, cmds.get, max_depth=3) is None
        )


class TestIsNoninteractiveInvocation:
    def test_entrypoint_sdk_cli_is_noninteractive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
        assert _is_noninteractive_invocation() is True

    def test_headless_claude_ancestor_is_noninteractive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
        monkeypatch.setattr(hook_mod.os, "getppid", lambda: 100)
        table = {
            100: {"ppid": "99", "command": "claude -p hi"},
            99: {"ppid": "1", "command": "/bin/zsh -c x"},
        }
        monkeypatch.setattr(
            hook_mod,
            "_ps_field",
            lambda pid, field: table.get(pid, {}).get(field),
        )
        assert _is_noninteractive_invocation() is True

    def test_interactive_claude_ancestor_is_interactive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
        monkeypatch.setattr(hook_mod.os, "getppid", lambda: 100)
        table = {100: {"ppid": "1", "command": "claude"}}
        monkeypatch.setattr(
            hook_mod,
            "_ps_field",
            lambda pid, field: table.get(pid, {}).get(field),
        )
        assert _is_noninteractive_invocation() is False

    def test_no_claude_ancestor_fail_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
        monkeypatch.setattr(hook_mod.os, "getppid", lambda: 10)
        table = {10: {"ppid": "1", "command": "-zsh"}}
        monkeypatch.setattr(
            hook_mod,
            "_ps_field",
            lambda pid, field: table.get(pid, {}).get(field),
        )
        assert _is_noninteractive_invocation() is False
