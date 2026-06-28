"""Hook subcommand for Claude Code session tracking.

Called by Claude Code's SessionStart hook to maintain a window↔session
mapping in <CCBOT_DIR>/session_map.json. Also provides `--install` to
auto-configure the hook in ~/.claude/settings.json.

This module must NOT import config.py (which requires TELEGRAM_BOT_TOKEN),
since hooks run inside tmux panes where bot env vars are not set.
Config directory resolution uses utils.ccbot_dir() (shared with config.py).

Key functions: hook_main() (CLI entry), _install_hook().
"""

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Validate session_id looks like a UUID
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Non-interactive Claude invocations that ccbot must NOT register as a
# window->session mapping. Only the interactive TUI that ccbot launches in a
# tmux window should own a window's session_map entry. A headless `claude -p` /
# SDK / background invocation spawned from inside an interactive session inherits
# that session's TMUX_PANE, so its SessionStart hook would otherwise overwrite
# session_map[<tmux>:<window_id>] and silently steal message delivery from the
# real session. (Incident: a daily-reminder `claude -p` run from a Bash tool
# inside the interactive session hijacked the mapping, dropping all later replies.)
#
# CLAUDE_CODE_CHILD_SESSION is deliberately NOT used as a signal: it is also set
# to "1" on legit interactive sessions whenever ccbot/tmux was itself started
# from within a Claude session, so it false-positives here. CLAUDE_CODE_ENTRYPOINT
# is undocumented, so it is only a secondary signal; the primary, documented
# signal is the `-p`/`--print`/`--bg`/`--remote-control` flag on the nearest
# claude ancestor's command line.
_HEADLESS_CLAUDE_FLAGS = frozenset({"-p", "--print", "--bg", "--remote-control"})
_HEADLESS_ENTRYPOINTS = frozenset({"sdk-cli", "sdk-py", "sdk-ts"})


def _executable_basename(cmdline: str) -> str:
    """Basename of the real executable in a process command line.

    Skips a leading `env` and its options / NAME=VALUE assignments (e.g.
    `env -u CLAUDECODE claude`) so the underlying program is identified.
    """
    tokens = cmdline.split()
    i = 0
    if i < len(tokens) and os.path.basename(tokens[i]) == "env":
        i += 1
        while i < len(tokens):
            t = tokens[i]
            if t == "-u":  # `-u NAME` removes a var — skip the flag and its argument
                i += 2
                continue
            if t.startswith("-") or "=" in t:
                i += 1
                continue
            break
    return os.path.basename(tokens[i]) if i < len(tokens) else ""


def _is_claude_process(cmdline: str) -> bool:
    """True if a command line is a `claude` executable.

    Guards against shells whose script text merely mentions "claude" by checking
    the resolved executable basename, not a substring.
    """
    return _executable_basename(cmdline) == "claude"


def _claude_cmdline_is_headless(cmdline: str) -> bool:
    """True if a `claude` command line carries a non-interactive (print/SDK/bg) flag."""
    return any(tok in _HEADLESS_CLAUDE_FLAGS for tok in cmdline.split())


def _ps_field(pid: int, field: str) -> str | None:
    """Read a single `ps -o <field>=` value for a pid, or None on failure."""
    try:
        out = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return out.stdout.strip() or None


def _find_nearest_claude_cmdline(
    start_pid: int,
    get_parent: Callable[[int], int | None],
    get_cmdline: Callable[[int], str | None],
    max_depth: int = 8,
) -> str | None:
    """Walk up the process tree from start_pid, returning the command line of the
    nearest ancestor that is a `claude` process (or None).

    "Nearest" matters: a headless `claude -p` run inside an interactive session
    has the `-p` claude as the nearest ancestor and the interactive claude higher
    up; checking only the nearest avoids misclassifying a legitimately nested
    interactive session.
    """
    pid: int | None = start_pid
    for _ in range(max_depth):
        if pid is None or pid <= 1:
            break
        cmdline = get_cmdline(pid)
        if cmdline and _is_claude_process(cmdline):
            return cmdline
        pid = get_parent(pid)
    return None


def _is_noninteractive_invocation() -> bool:
    """True if this SessionStart belongs to a non-interactive Claude invocation
    (headless `-p` / SDK / background) that ccbot must not register.

    Fail-open: when neither signal is conclusive, return False so behavior matches
    the historical default — a misfire reverts to old behavior, never breaks all
    tracking.
    """
    entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").strip()
    if entrypoint in _HEADLESS_ENTRYPOINTS:
        logger.info(
            "Skipping non-interactive Claude session (entrypoint=%s)", entrypoint
        )
        return True

    def _parent(pid: int) -> int | None:
        raw = _ps_field(pid, "ppid")
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    cmdline = _find_nearest_claude_cmdline(
        os.getppid(), _parent, lambda pid: _ps_field(pid, "command")
    )
    if cmdline and _claude_cmdline_is_headless(cmdline):
        logger.info(
            "Skipping non-interactive Claude session (claude cmdline: %s)", cmdline
        )
        return True
    return False


_CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

# The hook command suffix for detection
_HOOK_COMMAND_SUFFIX = "ccbot hook"


def _find_ccbot_path() -> str:
    """Find the full path to the ccbot executable.

    Priority:
    1. shutil.which("ccbot") - if ccbot is in PATH
    2. Same directory as the Python interpreter (for venv installs)
    """
    # Try PATH first
    ccbot_path = shutil.which("ccbot")
    if ccbot_path:
        return ccbot_path

    # Fall back to the directory containing the Python interpreter
    # This handles the case where ccbot is installed in a venv
    python_dir = Path(sys.executable).parent
    ccbot_in_venv = python_dir / "ccbot"
    if ccbot_in_venv.exists():
        return str(ccbot_in_venv)

    # Last resort: assume it will be in PATH
    return "ccbot"


def _find_existing_hook(settings: dict) -> tuple[dict, dict] | None:
    """Find an existing ccbot hook entry in settings.

    Returns (entry_dict, hook_dict) if found, None otherwise.
    Detects both 'ccbot hook' and full paths like '/path/to/ccbot hook'.
    """
    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", [])

    for entry in session_start:
        if not isinstance(entry, dict):
            continue
        inner_hooks = entry.get("hooks", [])
        for h in inner_hooks:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            # Match 'ccbot hook' or paths ending with 'ccbot hook'
            if cmd == _HOOK_COMMAND_SUFFIX or cmd.endswith("/" + _HOOK_COMMAND_SUFFIX):
                return entry, h
    return None


def _install_hook() -> int:
    """Install or update the ccbot hook in Claude's settings.json.

    If a ccbot hook already exists but points to a stale path,
    updates it to the current ccbot executable path.

    Returns 0 on success, 1 on error.
    """
    settings_file = _CLAUDE_SETTINGS_FILE
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    # Read existing settings
    settings: dict = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Error reading %s: %s", settings_file, e)
            print(f"Error reading {settings_file}: {e}", file=sys.stderr)
            return 1

    # Find the current ccbot path
    ccbot_path = _find_ccbot_path()
    hook_command = f"{ccbot_path} hook"

    # Check if hook already exists
    existing = _find_existing_hook(settings)
    if existing is not None:
        _, hook_dict = existing
        old_command = hook_dict.get("command", "")
        if old_command == hook_command:
            logger.info("Hook already installed in %s", settings_file)
            print(f"Hook already installed in {settings_file}")
            return 0
        # Path changed — update in place
        logger.info("Updating hook path: %s -> %s", old_command, hook_command)
        hook_dict["command"] = hook_command
    else:
        # Fresh install
        hook_config = {"type": "command", "command": hook_command, "timeout": 5}
        logger.info("Installing hook command: %s", hook_command)

        if "hooks" not in settings:
            settings["hooks"] = {}
        if "SessionStart" not in settings["hooks"]:
            settings["hooks"]["SessionStart"] = []

        settings["hooks"]["SessionStart"].append({"hooks": [hook_config]})

    # Write back
    try:
        settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        logger.error("Error writing %s: %s", settings_file, e)
        print(f"Error writing {settings_file}: {e}", file=sys.stderr)
        return 1

    action = "updated" if existing else "installed"
    logger.info("Hook %s successfully in %s", action, settings_file)
    print(f"Hook {action} successfully in {settings_file}")
    return 0


def hook_main() -> None:
    """Process a Claude Code hook event from stdin, or install the hook."""
    # Configure logging for the hook subprocess (main.py logging doesn't apply here)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.DEBUG,
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="ccbot hook",
        description="Claude Code session tracking hook",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install the hook into ~/.claude/settings.json",
    )
    # Parse only known args to avoid conflicts with stdin JSON
    args, _ = parser.parse_known_args(sys.argv[2:])

    if args.install:
        logger.info("Hook install requested")
        sys.exit(_install_hook())

    # Normal hook processing: read JSON from stdin
    logger.debug("Processing hook event from stdin")
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse stdin JSON: %s", e)
        return

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    event = payload.get("hook_event_name", "")

    if not session_id or not event:
        logger.debug("Empty session_id or event, ignoring")
        return

    # Validate session_id format
    if not _UUID_RE.match(session_id):
        logger.warning("Invalid session_id format: %s", session_id)
        return

    # Validate cwd is an absolute path (if provided)
    if cwd and not os.path.isabs(cwd):
        logger.warning("cwd is not absolute: %s", cwd)
        return

    if event != "SessionStart":
        logger.debug("Ignoring non-SessionStart event: %s", event)
        return

    # Ignore headless `claude -p` / SDK / background invocations. They can share
    # an interactive session's TMUX_PANE and would otherwise hijack that window's
    # session_map entry, silently breaking delivery. See _is_noninteractive_invocation.
    if _is_noninteractive_invocation():
        return

    # Get tmux session:window key for the pane running this hook.
    # TMUX_PANE is set by tmux for every process inside a pane.
    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        logger.warning("TMUX_PANE not set, cannot determine window")
        return

    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            pane_id,
            "-p",
            "#{session_name}:#{window_id}:#{window_name}",
        ],
        capture_output=True,
        text=True,
    )
    raw_output = result.stdout.strip()
    # Expected format: "session_name:@id:window_name"
    parts = raw_output.split(":", 2)
    if len(parts) < 3:
        logger.warning(
            "Failed to parse session:window_id:window_name from tmux (pane=%s, output=%s)",
            pane_id,
            raw_output,
        )
        return
    tmux_session_name, window_id, window_name = parts
    # Key uses window_id for uniqueness
    session_window_key = f"{tmux_session_name}:{window_id}"

    logger.debug(
        "tmux key=%s, window_name=%s, session_id=%s, cwd=%s",
        session_window_key,
        window_name,
        session_id,
        cwd,
    )

    # Read-modify-write with file locking to prevent concurrent hook races
    from .utils import ccbot_dir

    map_file = ccbot_dir() / "session_map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)

    lock_path = map_file.with_suffix(".lock")
    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            logger.debug("Acquired lock on %s", lock_path)
            try:
                session_map: dict[str, dict[str, str]] = {}
                if map_file.exists():
                    try:
                        session_map = json.loads(map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        logger.warning(
                            "Failed to read existing session_map, starting fresh"
                        )

                session_map[session_window_key] = {
                    "session_id": session_id,
                    "cwd": cwd,
                    "window_name": window_name,
                }

                # Clean up old-format key ("session:window_name") if it exists.
                # Previous versions keyed by window_name instead of window_id.
                old_key = f"{tmux_session_name}:{window_name}"
                if old_key != session_window_key and old_key in session_map:
                    del session_map[old_key]
                    logger.info("Removed old-format session_map key: %s", old_key)

                from .utils import atomic_write_json

                atomic_write_json(map_file, session_map)
                logger.info(
                    "Updated session_map: %s -> session_id=%s, cwd=%s",
                    session_window_key,
                    session_id,
                    cwd,
                )
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError as e:
        logger.error("Failed to write session_map: %s", e)
