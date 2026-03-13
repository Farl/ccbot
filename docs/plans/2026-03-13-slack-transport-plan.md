# Slack Transport Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Slack App (Socket Mode) as an alternative transport alongside Telegram, where each DM thread = one Claude Code session.

**Architecture:** Fork-based — Telegram code moves to `transports/telegram/`, Slack gets its own `transports/slack/` with forked handlers. Shared modules (session.py, session_monitor.py, etc.) get minimal changes. `--transport` CLI flag selects which to run.

**Tech Stack:** slack-bolt (Socket Mode), slack-sdk, Python 3.12, asyncio, tmux, uv

---

### Task 1: Add slack-bolt dependency

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add slack-bolt to optional dependencies**

In `pyproject.toml`, add a `slack` optional dependency group:

```toml
[project.optional-dependencies]
slack = [
    "slack-bolt>=1.18.0",
    "slack-sdk>=3.27.0",
]
```

**Step 2: Install and verify**

Run: `uv sync --extra slack`
Expected: slack-bolt and slack-sdk installed without errors

**Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add slack-bolt as optional dependency"
```

---

### Task 2: Create transports package structure

**Files:**
- Create: `src/ccbot/transports/__init__.py`
- Create: `src/ccbot/transports/telegram/__init__.py`
- Create: `src/ccbot/transports/slack/__init__.py`

**Step 1: Create the package directories and init files**

```python
# src/ccbot/transports/__init__.py
"""Transport layer — pluggable messaging backends (Telegram, Slack)."""

# src/ccbot/transports/telegram/__init__.py
"""Telegram transport — python-telegram-bot based backend."""

# src/ccbot/transports/slack/__init__.py
"""Slack transport — slack-bolt Socket Mode backend."""
```

**Step 2: Verify imports work**

Run: `python -c "import ccbot.transports; import ccbot.transports.telegram; import ccbot.transports.slack"`
Expected: No errors

**Step 3: Commit**

```bash
git add src/ccbot/transports/
git commit -m "chore: create transports package structure"
```

---

### Task 3: Move Telegram code into transports/telegram/

This is the largest task — a pure file move + import path update. No logic changes.

**Files:**
- Move: `src/ccbot/bot.py` → `src/ccbot/transports/telegram/bot.py`
- Move: `src/ccbot/handlers/` → `src/ccbot/transports/telegram/handlers/`
- Move: `src/ccbot/markdown_v2.py` → `src/ccbot/transports/telegram/markdown_v2.py`
- Move: `src/ccbot/telegram_sender.py` → `src/ccbot/transports/telegram/telegram_sender.py`
- Modify: All moved files (update relative imports)
- Modify: `src/ccbot/main.py` (update import path)

**Step 1: Move the files using git mv**

```bash
# Move bot.py
git mv src/ccbot/bot.py src/ccbot/transports/telegram/bot.py

# Move handlers directory
git mv src/ccbot/handlers src/ccbot/transports/telegram/handlers

# Move markdown_v2.py
git mv src/ccbot/markdown_v2.py src/ccbot/transports/telegram/markdown_v2.py

# Move telegram_sender.py
git mv src/ccbot/telegram_sender.py src/ccbot/transports/telegram/telegram_sender.py
```

**Step 2: Update imports in all moved files**

All files that used relative imports like `from ..config import config` now need to go one more level up: `from ...config import config`.

Key pattern changes in moved files:
- `from .config` → `from ...config`
- `from .session` → `from ...session`
- `from .tmux_manager` → `from ...tmux_manager`
- `from .terminal_parser` → `from ...terminal_parser`
- `from .session_monitor` → `from ...session_monitor`
- `from .transcript_parser` → `from ...transcript_parser`
- `from .screenshot` → `from ...screenshot`
- `from .utils` → `from ...utils`

In `transports/telegram/bot.py`:
- `from .handlers.xxx` → `from .handlers.xxx` (stays same, handlers moved together)
- `from .config` → `from ...config`
- `from .markdown_v2` → `from .markdown_v2` (stays same, moved together)

In `transports/telegram/handlers/*.py`:
- `from ..config` → `from ....config` (was 2 levels, now 4 — handler → telegram → transports → ccbot)
- Wait, let's reconsider. Handler files currently use `from ..config` (up from handlers/ to ccbot/). After move, they are at `transports/telegram/handlers/`, so need `from ....config` (up 3 to ccbot/).

Actually the pattern is:
- Handlers currently: `src/ccbot/handlers/xxx.py` → `from ..module` goes to `src/ccbot/module`
- Handlers after move: `src/ccbot/transports/telegram/handlers/xxx.py` → `from ....module` goes to `src/ccbot/module`

So `..` becomes `....` in handler files for shared modules.

For handler-internal imports (e.g. `from .message_sender import ...`), those stay as `.` since they moved together.

For `from ..markdown_v2` in handlers, it becomes `from ..markdown_v2` (still same — goes up to telegram/).

**Step 3: Update main.py**

```python
# Change:
from .bot import create_bot
# To:
from .transports.telegram.bot import create_bot
```

**Step 4: Verify**

Run: `uv run ruff check src/`
Run: `uv run pyright src/ccbot/`
Run: `uv run pytest tests/ -x`
Expected: All pass. If import errors, fix them.

**Step 5: Commit**

```bash
git add -A
git commit -m "refactor: move Telegram code into transports/telegram/"
```

---

### Task 4: Update config.py for transport selection

**Files:**
- Modify: `src/ccbot/config.py`

**Step 1: Make Telegram token optional, add Slack tokens**

The config should not crash if `TELEGRAM_BOT_TOKEN` is missing (user might only use Slack). Add Slack config fields.

```python
# In Config.__init__, replace the hard error for TELEGRAM_BOT_TOKEN:
self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
# Remove the ValueError raise — validation happens at transport startup

# Add Slack config:
self.slack_bot_token: str = os.getenv("SLACK_BOT_TOKEN") or ""
self.slack_app_token: str = os.getenv("SLACK_APP_TOKEN") or ""

# Add to SENSITIVE_ENV_VARS:
SENSITIVE_ENV_VARS = {"TELEGRAM_BOT_TOKEN", "ALLOWED_USERS", "OPENAI_API_KEY", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"}

# Make ALLOWED_USERS accept string user IDs (Slack uses strings like "U0123ABC"):
# Change parsing to store as set[str] instead of set[int]
allowed_users_str = os.getenv("ALLOWED_USERS", "")
if not allowed_users_str:
    raise ValueError("ALLOWED_USERS environment variable is required")
self.allowed_users: set[str] = {
    uid.strip() for uid in allowed_users_str.split(",") if uid.strip()
}

def is_user_allowed(self, user_id: int | str) -> bool:
    return str(user_id) in self.allowed_users
```

**Step 2: Update all callers of config.allowed_users and is_user_allowed**

Search for `config.allowed_users` and `config.is_user_allowed` — update any int comparisons to use `str()`.

Check `transports/telegram/bot.py` — the user ID check likely does `config.is_user_allowed(user.id)` where `user.id` is int. With the new `is_user_allowed(int | str)`, this still works via `str(user_id)`.

**Step 3: Verify**

Run: `uv run ruff check src/ && uv run pyright src/ccbot/`
Run: `uv run pytest tests/ -x`

**Step 4: Commit**

```bash
git add src/ccbot/config.py src/ccbot/transports/telegram/
git commit -m "feat: make config transport-agnostic, add Slack token support"
```

---

### Task 5: Update main.py with --transport flag

**Files:**
- Modify: `src/ccbot/main.py`

**Step 1: Add argparse for transport selection**

```python
import argparse
import logging
import sys


def main() -> None:
    """Main entry point."""
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        from .hook import hook_main
        hook_main()
        return

    parser = argparse.ArgumentParser(description="CCBot - Claude Code Bot")
    parser.add_argument(
        "--transport",
        choices=["telegram", "slack"],
        default="telegram",
        help="Messaging transport to use (default: telegram)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    try:
        from .config import config
    except ValueError as e:
        from .utils import ccbot_dir
        config_dir = ccbot_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Create {env_path} with required env vars.")
        sys.exit(1)

    logging.getLogger("ccbot").setLevel(logging.DEBUG)
    logger = logging.getLogger(__name__)

    from .tmux_manager import tmux_manager
    logger.info("Allowed users: %s", config.allowed_users)
    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    if args.transport == "telegram":
        if not config.telegram_bot_token:
            print("Error: TELEGRAM_BOT_TOKEN required for telegram transport")
            sys.exit(1)
        logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
        logger.info("Starting Telegram bot...")
        from .transports.telegram.bot import create_bot
        application = create_bot()
        application.run_polling(allowed_updates=["message", "callback_query"])
    elif args.transport == "slack":
        if not config.slack_bot_token or not config.slack_app_token:
            print("Error: SLACK_BOT_TOKEN and SLACK_APP_TOKEN required for slack transport")
            sys.exit(1)
        logger.info("Starting Slack bot...")
        from .transports.slack.bot import run_slack_bot
        run_slack_bot()
```

**Step 2: Verify Telegram still works**

Run: `uv run ccbot --transport telegram` (should behave exactly as before)
Run: `uv run ccbot --help` (should show --transport option)

**Step 3: Commit**

```bash
git add src/ccbot/main.py
git commit -m "feat: add --transport CLI flag for Telegram/Slack selection"
```

---

### Task 6: Update session.py for string thread IDs

**Files:**
- Modify: `src/ccbot/session.py`
- Modify: `tests/ccbot/test_session.py` (if exists)

**Step 1: Change thread_bindings type from dict[int, dict[int, str]] to dict[str, dict[str, str]]**

Key changes in `session.py`:

```python
# Type change:
thread_bindings: dict[str, dict[str, str]] = field(default_factory=dict)

# _load_state: keep int parsing for backward compat, but store as str
self.thread_bindings = {
    str(uid): {str(tid): wid for tid, wid in bindings.items()}
    for uid, bindings in state.get("thread_bindings", {}).items()
}

# _save_state: already uses str() conversion, just keep it
"thread_bindings": {
    str(uid): {str(tid): wid for tid, wid in bindings.items()}
    for uid, bindings in self.thread_bindings.items()
},

# All method signatures: int → str
def bind_thread(self, user_id: str, thread_id: str, window_id: str, ...) -> None:
def unbind_thread(self, user_id: str, thread_id: str) -> str | None:
def get_window_for_thread(self, user_id: str, thread_id: str) -> str | None:
def resolve_window_for_thread(self, user_id: str, thread_id: str | None) -> str | None:
def iter_thread_bindings(self) -> Iterator[tuple[str, str, str]]:
def find_users_for_session(self, session_id: str) -> list[tuple[str, str, str]]:

# user_window_offsets: also change key from int to str
user_window_offsets: dict[str, dict[str, int]] = field(default_factory=dict)

# group_chat_ids: keep as-is (Telegram-specific)

# resolve_chat_id and set_group_chat_id: change user_id param to str
def set_group_chat_id(self, user_id: str, thread_id: str | None, chat_id: int) -> None:
def resolve_chat_id(self, user_id: str, thread_id: str | None = None) -> int:
```

**Step 2: Update all callers in transports/telegram/ to pass str(user_id), str(thread_id)**

Search for calls to `bind_thread`, `unbind_thread`, `get_window_for_thread`, `resolve_window_for_thread`, `find_users_for_session`, `iter_thread_bindings`, `update_user_window_offset`, `set_group_chat_id`, `resolve_chat_id` in the Telegram transport files and ensure all int arguments are wrapped with `str()`.

**Step 3: Verify**

Run: `uv run ruff check src/ && uv run pyright src/ccbot/`
Run: `uv run pytest tests/ -x`

**Step 4: Commit**

```bash
git add src/ccbot/session.py src/ccbot/transports/telegram/
git commit -m "refactor: use string keys in session thread_bindings for transport flexibility"
```

---

### Task 7: Slack formatter — markdown to mrkdwn

**Files:**
- Create: `src/ccbot/transports/slack/formatter.py`
- Create: `tests/ccbot/transports/__init__.py`
- Create: `tests/ccbot/transports/slack/__init__.py`
- Create: `tests/ccbot/transports/slack/test_formatter.py`

**Step 1: Write the failing tests**

```python
# tests/ccbot/transports/slack/test_formatter.py
"""Tests for Slack mrkdwn formatter."""

from ccbot.transports.slack.formatter import to_mrkdwn


def test_bold():
    assert to_mrkdwn("**bold**") == "*bold*"


def test_italic():
    assert to_mrkdwn("*italic*") == "_italic_"


def test_code_inline():
    assert to_mrkdwn("`code`") == "`code`"


def test_code_block():
    assert to_mrkdwn("```python\nprint('hi')\n```") == "```\nprint('hi')\n```"


def test_link():
    assert to_mrkdwn("[text](http://example.com)") == "<http://example.com|text>"


def test_heading():
    assert to_mrkdwn("## Heading") == "*Heading*"


def test_mixed():
    text = "**bold** and *italic* with `code`"
    result = to_mrkdwn(text)
    assert "*bold*" in result
    assert "_italic_" in result
    assert "`code`" in result


def test_passthrough_plain():
    assert to_mrkdwn("hello world") == "hello world"


def test_strikethrough():
    assert to_mrkdwn("~~deleted~~") == "~deleted~"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ccbot/transports/slack/test_formatter.py -v`
Expected: ImportError (module doesn't exist yet)

**Step 3: Implement the formatter**

```python
# src/ccbot/transports/slack/formatter.py
"""Markdown to Slack mrkdwn converter.

Converts standard Markdown (as output by Claude) to Slack's mrkdwn format.
Differences: **bold** → *bold*, *italic* → _italic_, [text](url) → <url|text>,
code blocks lose language specifier, headings → bold text.
"""

import re


def to_mrkdwn(text: str) -> str:
    """Convert Markdown to Slack mrkdwn format."""
    # Protect code blocks from other transformations
    code_blocks: list[str] = []

    def _save_code_block(m: re.Match) -> str:
        # Strip language specifier from fenced code blocks
        code = m.group(1)
        code_blocks.append(f"```\n{code}\n```")
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    result = re.sub(r"```\w*\n(.*?)```", _save_code_block, text, flags=re.DOTALL)

    # Protect inline code
    inline_codes: list[str] = []

    def _save_inline(m: re.Match) -> str:
        inline_codes.append(m.group(0))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    result = re.sub(r"`[^`]+`", _save_inline, result)

    # Headings → bold
    result = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", result, flags=re.MULTILINE)

    # Bold: **text** → *text*
    result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)

    # Italic: *text* → _text_ (but not inside bold)
    # Match single * not preceded/followed by *
    result = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"_\1_", result)

    # Strikethrough: ~~text~~ → ~text~
    result = re.sub(r"~~(.+?)~~", r"~\1~", result)

    # Links: [text](url) → <url|text>
    result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", result)

    # Restore code blocks and inline code
    for i, code in enumerate(code_blocks):
        result = result.replace(f"\x00CODEBLOCK{i}\x00", code)
    for i, code in enumerate(inline_codes):
        result = result.replace(f"\x00INLINE{i}\x00", code)

    return result
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ccbot/transports/slack/test_formatter.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/ccbot/transports/slack/formatter.py tests/ccbot/transports/
git commit -m "feat: add Slack mrkdwn formatter with tests"
```

---

### Task 8: Slack message splitter

**Files:**
- Create: `src/ccbot/transports/slack/splitter.py`
- Create: `tests/ccbot/transports/slack/test_splitter.py`

**Step 1: Write the failing tests**

```python
# tests/ccbot/transports/slack/test_splitter.py
"""Tests for Slack message splitter."""

from ccbot.transports.slack.splitter import split_message


def test_short_message():
    parts = split_message("hello", max_length=4000)
    assert parts == ["hello"]


def test_long_message_splits():
    text = "a" * 8000
    parts = split_message(text, max_length=4000)
    assert len(parts) == 2
    assert all(len(p) <= 4000 for p in parts)
    assert "".join(parts) == text


def test_split_at_newline():
    text = "line1\n" + "a" * 3990 + "\nline3"
    parts = split_message(text, max_length=4000)
    assert len(parts) >= 2
    assert all(len(p) <= 4000 for p in parts)


def test_preserves_code_blocks():
    code = "```\n" + "x" * 100 + "\n```"
    text = "before\n" + code + "\nafter"
    parts = split_message(text, max_length=4000)
    # Code block should not be split
    full = "\n".join(parts)
    assert "```" in full
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ccbot/transports/slack/test_splitter.py -v`
Expected: ImportError

**Step 3: Implement the splitter**

```python
# src/ccbot/transports/slack/splitter.py
"""Message splitting for Slack's 4000-character limit.

Splits long messages at newline boundaries, preserving code block integrity.
"""

SLACK_MAX_LENGTH = 4000


def split_message(text: str, max_length: int = SLACK_MAX_LENGTH) -> list[str]:
    """Split text into chunks that fit within Slack's message limit.

    Tries to split at newlines. Falls back to hard split if a single line
    exceeds max_length.
    """
    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    current = ""

    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_length:
            current = candidate
        else:
            if current:
                parts.append(current)
            # Handle single line longer than max
            if len(line) > max_length:
                for i in range(0, len(line), max_length):
                    parts.append(line[i : i + max_length])
                current = ""
            else:
                current = line

    if current:
        parts.append(current)

    return parts
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ccbot/transports/slack/test_splitter.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/ccbot/transports/slack/splitter.py tests/ccbot/transports/slack/test_splitter.py
git commit -m "feat: add Slack message splitter with tests"
```

---

### Task 9: Slack message sender

**Files:**
- Create: `src/ccbot/transports/slack/handlers/__init__.py`
- Create: `src/ccbot/transports/slack/handlers/message_sender.py`

**Step 1: Implement the sender**

This is the core Slack API wrapper. It wraps `slack_sdk.web.async_client.AsyncWebClient`.

```python
# src/ccbot/transports/slack/handlers/message_sender.py
"""Slack message sending helpers.

Wraps slack_sdk AsyncWebClient for sending, editing, and deleting messages.
Handles mrkdwn formatting and fallback to plain text.
"""

import logging
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from ..formatter import to_mrkdwn
from ..splitter import split_message

logger = logging.getLogger(__name__)


async def send_message(
    client: AsyncWebClient,
    channel: str,
    text: str,
    thread_ts: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
) -> str | None:
    """Send a message to a Slack channel/thread.

    Returns the message ts (ID) on success, None on failure.
    """
    try:
        formatted = to_mrkdwn(text)
        kwargs: dict[str, Any] = {
            "channel": channel,
            "text": formatted,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if blocks:
            kwargs["blocks"] = blocks
        response = await client.chat_postMessage(**kwargs)
        return response.get("ts")
    except Exception as e:
        logger.error("Failed to send message to %s: %s", channel, e)
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
        await client.chat_update(**kwargs)
        return True
    except Exception as e:
        logger.error("Failed to edit message %s: %s", ts, e)
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
        logger.error("Failed to delete message %s: %s", ts, e)
        return False


async def send_long_message(
    client: AsyncWebClient,
    channel: str,
    text: str,
    thread_ts: str | None = None,
) -> str | None:
    """Send a long message, splitting if needed. Returns first message ts."""
    parts = split_message(text)
    first_ts = None
    for part in parts:
        ts = await send_message(client, channel, part, thread_ts=thread_ts)
        if first_ts is None:
            first_ts = ts
    return first_ts
```

**Step 2: Verify syntax**

Run: `uv run ruff check src/ccbot/transports/slack/`
Expected: Pass

**Step 3: Commit**

```bash
git add src/ccbot/transports/slack/handlers/
git commit -m "feat: add Slack message sender with formatting and splitting"
```

---

### Task 10: Slack bot core — Socket Mode app + event handling

**Files:**
- Create: `src/ccbot/transports/slack/bot.py`

**Step 1: Implement the Slack bot**

This is the main entry point for the Slack transport. It sets up Socket Mode, registers event listeners, and starts the session monitor.

```python
# src/ccbot/transports/slack/bot.py
"""Slack bot — Socket Mode app with event listeners.

Registers message handlers and block_actions for the Slack transport.
Each DM thread maps 1:1 to a tmux window (Claude session).
"""

import asyncio
import logging

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from ...config import config
from ...session import session_manager
from ...session_monitor import SessionMonitor, NewMessage
from ...tmux_manager import tmux_manager
from .handlers.message_sender import send_message, send_long_message

logger = logging.getLogger(__name__)

# Module-level app instance
app = AsyncApp(token=config.slack_bot_token)

# DM channel cache: user_id -> channel_id
_dm_channels: dict[str, str] = {}


async def _resolve_dm_channel(user_id: str) -> str | None:
    """Get or open a DM channel with a user."""
    if user_id in _dm_channels:
        return _dm_channels[user_id]
    try:
        response = await app.client.conversations_open(users=[user_id])
        channel_id = response["channel"]["id"]
        _dm_channels[user_id] = channel_id
        return channel_id
    except Exception as e:
        logger.error("Failed to open DM with %s: %s", user_id, e)
        return None


def _is_bot_message(event: dict) -> bool:
    """Check if a message event is from the bot itself."""
    return event.get("bot_id") is not None or event.get("subtype") == "bot_message"


@app.event("message")
async def handle_message(event: dict, say) -> None:
    """Handle incoming DM messages."""
    # Ignore bot's own messages
    if _is_bot_message(event):
        return

    user_id = event.get("user", "")
    if not config.is_user_allowed(user_id):
        return

    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts")  # None if top-level message
    text = event.get("text", "")

    if not text:
        return

    # If no thread_ts, this is a top-level message → start directory browser
    if not thread_ts:
        # For now, just acknowledge. Directory browser will be Task 12.
        await say(
            text="Send messages inside a thread to interact with Claude. "
            "Use the directory browser to create a new session.",
            channel=channel,
        )
        return

    # Look up thread binding
    window_id = session_manager.resolve_window_for_thread(user_id, thread_ts)
    if not window_id:
        await say(
            text="This thread is not bound to a session. Start a new thread to create one.",
            channel=channel,
            thread_ts=thread_ts,
        )
        return

    # Forward text to tmux window
    success, msg = await session_manager.send_to_window(window_id, text)
    if not success:
        await say(text=f"Failed to send: {msg}", channel=channel, thread_ts=thread_ts)


async def handle_new_message(msg: NewMessage) -> None:
    """Callback for session monitor — deliver Claude's messages to Slack threads."""
    users = await session_manager.find_users_for_session(msg.session_id)
    for user_id, _window_id, thread_id in users:
        channel = await _resolve_dm_channel(user_id)
        if not channel:
            continue
        await send_long_message(
            app.client, channel, msg.text, thread_ts=thread_id
        )


def run_slack_bot() -> None:
    """Start the Slack bot with Socket Mode."""
    async def _run() -> None:
        # Resolve stale window IDs
        await session_manager.resolve_stale_ids()

        # Start session monitor
        monitor = SessionMonitor()
        monitor.set_message_callback(handle_new_message)
        monitor.start()

        logger.info("Starting Slack bot in Socket Mode...")
        handler = AsyncSocketModeHandler(app, config.slack_app_token)
        await handler.start_async()

    asyncio.run(_run())
```

**Step 2: Verify syntax**

Run: `uv run ruff check src/ccbot/transports/slack/bot.py`
Expected: Pass (may warn about unused imports — that's fine for now)

**Step 3: Commit**

```bash
git add src/ccbot/transports/slack/bot.py
git commit -m "feat: add Slack bot core with Socket Mode and message handling"
```

---

### Task 11: Slack status polling

**Files:**
- Create: `src/ccbot/transports/slack/handlers/status_polling.py`

**Step 1: Implement status polling**

Fork from Telegram's status_polling.py, replace Telegram API calls with Slack API calls.

```python
# src/ccbot/transports/slack/handlers/status_polling.py
"""Background status line polling for Slack transport.

Polls terminal status for all active windows at 1-second intervals.
Sends/edits status messages in the appropriate Slack threads.
"""

import asyncio
import logging

from slack_sdk.web.async_client import AsyncWebClient

from ....session import session_manager
from ....terminal_parser import is_interactive_ui, parse_status_line
from ....tmux_manager import tmux_manager
from ..handlers.message_sender import send_message, edit_message, delete_message

logger = logging.getLogger(__name__)

# Status message tracking: (user_id, thread_ts) -> (msg_ts, window_id, last_text)
_status_msgs: dict[tuple[str, str], tuple[str, str, str]] = {}

STATUS_POLL_INTERVAL = 1.0


async def update_status_for_window(
    client: AsyncWebClient,
    user_id: str,
    thread_ts: str,
    window_id: str,
    channel: str,
) -> None:
    """Poll and update status for a single window."""
    pane_text = await tmux_manager.capture_pane(window_id, last_n_lines=20)
    if not pane_text:
        return

    status = parse_status_line(pane_text)
    if not status:
        # No status line — clear any existing status message
        key = (user_id, thread_ts)
        info = _status_msgs.pop(key, None)
        if info:
            await delete_message(client, channel, info[0])
        return

    status_text = f"⏳ {status}"
    key = (user_id, thread_ts)
    existing = _status_msgs.get(key)

    if existing:
        msg_ts, stored_wid, last_text = existing
        if stored_wid == window_id and last_text == status_text:
            return  # No change, skip
        # Update existing status message
        success = await edit_message(client, channel, msg_ts, status_text)
        if success:
            _status_msgs[key] = (msg_ts, window_id, status_text)
    else:
        # Send new status message
        msg_ts = await send_message(
            client, channel, status_text, thread_ts=thread_ts
        )
        if msg_ts:
            _status_msgs[key] = (msg_ts, window_id, status_text)


async def clear_status(user_id: str, thread_ts: str, client: AsyncWebClient, channel: str) -> None:
    """Clear status message for a thread."""
    key = (user_id, thread_ts)
    info = _status_msgs.pop(key, None)
    if info:
        await delete_message(client, channel, info[0])


async def start_status_polling(client: AsyncWebClient, get_channel_for_user) -> None:
    """Background task that polls status for all bound threads.

    Args:
        client: Slack AsyncWebClient
        get_channel_for_user: async callable(user_id) -> channel_id
    """
    while True:
        try:
            for user_id, thread_ts, window_id in session_manager.iter_thread_bindings():
                window = await tmux_manager.find_window_by_id(window_id)
                if not window:
                    continue
                channel = await get_channel_for_user(user_id)
                if not channel:
                    continue
                await update_status_for_window(
                    client, user_id, thread_ts, window_id, channel
                )
        except Exception as e:
            logger.error("Status polling error: %s", e)

        await asyncio.sleep(STATUS_POLL_INTERVAL)
```

**Step 2: Verify syntax**

Run: `uv run ruff check src/ccbot/transports/slack/handlers/status_polling.py`

**Step 3: Commit**

```bash
git add src/ccbot/transports/slack/handlers/status_polling.py
git commit -m "feat: add Slack status polling handler"
```

---

### Task 12: Slack directory browser

**Files:**
- Create: `src/ccbot/transports/slack/handlers/directory_browser.py`

**Step 1: Implement directory browser with Block Kit**

The directory browser lets users pick a directory to start a new Claude session. Uses Slack Block Kit buttons instead of Telegram inline keyboards.

```python
# src/ccbot/transports/slack/handlers/directory_browser.py
"""Directory browser UI for Slack — select a directory to create a new session.

Uses Slack Block Kit buttons for navigation. Users browse the filesystem
and select a directory, which creates a new tmux window + Claude session.
"""

import logging
from pathlib import Path
from typing import Any

from ....config import config
from ....tmux_manager import tmux_manager
from ....session import session_manager

logger = logging.getLogger(__name__)

# Callback action_id prefixes
ACTION_DIR_SELECT = "dir_select_"
ACTION_DIR_UP = "dir_up"
ACTION_DIR_CONFIRM = "dir_confirm"
ACTION_DIR_CANCEL = "dir_cancel"
ACTION_DIR_PAGE = "dir_page_"

DIRS_PER_PAGE = 8

# Per-user browse state: user_id -> {path, page, dirs}
_browse_state: dict[str, dict[str, Any]] = {}


def _list_dirs(path: Path) -> list[str]:
    """List subdirectories, respecting show_hidden_dirs config."""
    try:
        dirs = sorted(
            d.name
            for d in path.iterdir()
            if d.is_dir()
            and (config.show_hidden_dirs or not d.name.startswith("."))
            and d.name not in {"node_modules", "__pycache__", ".git"}
        )
        return dirs
    except PermissionError:
        return []


def build_directory_browser(user_id: str, path: Path | None = None, page: int = 0) -> dict:
    """Build Block Kit blocks for directory browsing.

    Returns a dict with 'text' and 'blocks' for Slack message.
    """
    if path is None:
        path = Path.home()

    dirs = _list_dirs(path)
    _browse_state[user_id] = {"path": str(path), "page": page, "dirs": dirs}

    start = page * DIRS_PER_PAGE
    end = start + DIRS_PER_PAGE
    page_dirs = dirs[start:end]
    total_pages = max(1, (len(dirs) + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*📁 {path}*\nPage {page + 1}/{total_pages}"},
        }
    ]

    # Directory buttons (2 per row)
    for i in range(0, len(page_dirs), 2):
        elements = []
        for j in range(2):
            if i + j < len(page_dirs):
                d = page_dirs[i + j]
                elements.append({
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"📁 {d}"[:75]},
                    "action_id": f"{ACTION_DIR_SELECT}{start + i + j}",
                })
        blocks.append({"type": "actions", "elements": elements})

    # Navigation buttons
    nav_elements = []
    if str(path) != str(path.root):
        nav_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "⬆️ Parent"},
            "action_id": ACTION_DIR_UP,
        })
    if page > 0:
        nav_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "◀️ Prev"},
            "action_id": f"{ACTION_DIR_PAGE}{page - 1}",
        })
    if end < len(dirs):
        nav_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "▶️ Next"},
            "action_id": f"{ACTION_DIR_PAGE}{page + 1}",
        })
    nav_elements.append({
        "type": "button",
        "text": {"type": "plain_text", "text": "✅ Select this directory"},
        "action_id": ACTION_DIR_CONFIRM,
        "style": "primary",
    })
    nav_elements.append({
        "type": "button",
        "text": {"type": "plain_text", "text": "❌ Cancel"},
        "action_id": ACTION_DIR_CANCEL,
        "style": "danger",
    })
    blocks.append({"type": "actions", "elements": nav_elements})

    return {
        "text": f"Browse: {path}",
        "blocks": blocks,
    }


def get_browse_state(user_id: str) -> dict[str, Any] | None:
    """Get the current browse state for a user."""
    return _browse_state.get(user_id)


def clear_browse_state(user_id: str) -> None:
    """Clear browse state for a user."""
    _browse_state.pop(user_id, None)


async def create_session_for_thread(
    user_id: str,
    thread_ts: str,
    directory: str,
) -> str | None:
    """Create a new tmux window + Claude session and bind to thread.

    Returns window_id on success, None on failure.
    """
    path = Path(directory)
    window_name = path.name or "root"

    window = await tmux_manager.create_window(
        window_name=window_name,
        start_directory=directory,
    )
    if not window:
        return None

    session_manager.bind_thread(user_id, thread_ts, window.window_id, window_name)
    session_manager.update_display_name(window.window_id, window_name)

    # Wait for session_map entry (hook fires on Claude startup)
    await session_manager.wait_for_session_map_entry(window.window_id)

    return window.window_id
```

**Step 2: Verify syntax**

Run: `uv run ruff check src/ccbot/transports/slack/handlers/directory_browser.py`

**Step 3: Commit**

```bash
git add src/ccbot/transports/slack/handlers/directory_browser.py
git commit -m "feat: add Slack directory browser with Block Kit UI"
```

---

### Task 13: Slack interactive UI (Block Kit buttons)

**Files:**
- Create: `src/ccbot/transports/slack/handlers/interactive_ui.py`

**Step 1: Implement interactive UI**

Handles permission prompts, AskUserQuestion, etc. via Block Kit navigation buttons.

```python
# src/ccbot/transports/slack/handlers/interactive_ui.py
"""Interactive UI handling for Slack — permission prompts, navigation.

Detects interactive UIs in terminal output and presents Block Kit buttons
for user interaction (arrow keys, Enter, Esc, etc.).
"""

import logging
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from ....terminal_parser import is_interactive_ui, parse_interactive_content
from ..handlers.message_sender import send_message, edit_message, delete_message

logger = logging.getLogger(__name__)

# Track interactive messages: (user_id, thread_ts) -> msg_ts
_interactive_msgs: dict[tuple[str, str], str] = {}

# Track interactive mode: (user_id, thread_ts) -> window_id
_interactive_mode: dict[tuple[str, str], str] = {}

# Grace period for clearing
CLEAR_GRACE_MISSES = 3
_miss_counts: dict[tuple[str, str], int] = {}

# Action ID prefixes
ACTION_NAV_PREFIX = "nav_"


def get_interactive_window(user_id: str, thread_ts: str) -> str | None:
    """Get the window_id for user's interactive mode."""
    return _interactive_mode.get((user_id, thread_ts))


def build_nav_keyboard(window_id: str) -> list[dict[str, Any]]:
    """Build Block Kit navigation buttons for interactive UI."""
    def btn(text: str, action: str) -> dict:
        return {
            "type": "button",
            "text": {"type": "plain_text", "text": text},
            "action_id": f"{ACTION_NAV_PREFIX}{action}_{window_id}",
        }

    return [
        {
            "type": "actions",
            "elements": [btn("Space", "space"), btn("⬆️", "up"), btn("Tab", "tab")],
        },
        {
            "type": "actions",
            "elements": [btn("⬅️", "left"), btn("⬇️", "down"), btn("➡️", "right")],
        },
        {
            "type": "actions",
            "elements": [btn("Esc", "esc"), btn("🔄", "refresh"), btn("Enter", "enter")],
        },
    ]


async def handle_interactive_ui(
    client: AsyncWebClient,
    user_id: str,
    thread_ts: str,
    window_id: str,
    channel: str,
    pane_text: str,
) -> None:
    """Send or update interactive UI message."""
    key = (user_id, thread_ts)
    _interactive_mode[key] = window_id

    # Extract the visible UI content from pane
    content = parse_interactive_content(pane_text) if hasattr(parse_interactive_content, '__call__') else pane_text[-500:]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```\n{content}\n```"}},
        *build_nav_keyboard(window_id),
    ]

    existing_ts = _interactive_msgs.get(key)
    if existing_ts:
        await edit_message(client, channel, existing_ts, content, blocks=blocks)
    else:
        msg_ts = await send_message(
            client, channel, content, thread_ts=thread_ts, blocks=blocks
        )
        if msg_ts:
            _interactive_msgs[key] = msg_ts


async def clear_interactive_msg(
    client: AsyncWebClient,
    user_id: str,
    thread_ts: str,
    channel: str,
) -> None:
    """Clear interactive UI message and mode."""
    key = (user_id, thread_ts)
    _interactive_mode.pop(key, None)
    _miss_counts.pop(key, None)
    msg_ts = _interactive_msgs.pop(key, None)
    if msg_ts:
        await delete_message(client, channel, msg_ts)
```

**Step 2: Verify syntax**

Run: `uv run ruff check src/ccbot/transports/slack/handlers/interactive_ui.py`

**Step 3: Commit**

```bash
git add src/ccbot/transports/slack/handlers/interactive_ui.py
git commit -m "feat: add Slack interactive UI with Block Kit navigation"
```

---

### Task 14: Wire up block_actions in Slack bot

**Files:**
- Modify: `src/ccbot/transports/slack/bot.py`

**Step 1: Add block_actions handler for directory browser + interactive UI**

Add to `bot.py`:

```python
from .handlers.directory_browser import (
    ACTION_DIR_SELECT,
    ACTION_DIR_UP,
    ACTION_DIR_CONFIRM,
    ACTION_DIR_CANCEL,
    ACTION_DIR_PAGE,
    build_directory_browser,
    get_browse_state,
    clear_browse_state,
    create_session_for_thread,
)
from .handlers.interactive_ui import (
    ACTION_NAV_PREFIX,
)

# Register block_actions handlers:

@app.action(re.compile(r"^dir_"))
async def handle_dir_action(ack, body, client):
    """Handle directory browser button clicks."""
    await ack()
    action = body["actions"][0]
    action_id = action["action_id"]
    user_id = body["user"]["id"]
    channel = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    thread_ts = body["message"].get("thread_ts")

    state = get_browse_state(user_id)
    if not state:
        return

    current_path = Path(state["path"])

    if action_id.startswith(ACTION_DIR_SELECT):
        idx = int(action_id.replace(ACTION_DIR_SELECT, ""))
        dirs = state["dirs"]
        if 0 <= idx < len(dirs):
            new_path = current_path / dirs[idx]
            browser = build_directory_browser(user_id, new_path)
            await client.chat_update(
                channel=channel, ts=message_ts,
                text=browser["text"], blocks=browser["blocks"],
            )

    elif action_id == ACTION_DIR_UP:
        browser = build_directory_browser(user_id, current_path.parent)
        await client.chat_update(
            channel=channel, ts=message_ts,
            text=browser["text"], blocks=browser["blocks"],
        )

    elif action_id.startswith(ACTION_DIR_PAGE):
        page = int(action_id.replace(ACTION_DIR_PAGE, ""))
        browser = build_directory_browser(user_id, current_path, page)
        await client.chat_update(
            channel=channel, ts=message_ts,
            text=browser["text"], blocks=browser["blocks"],
        )

    elif action_id == ACTION_DIR_CONFIRM:
        clear_browse_state(user_id)
        # Create session — need a thread. If no thread_ts, create one.
        if not thread_ts:
            # Post a top-level message to start the thread
            resp = await client.chat_postMessage(
                channel=channel,
                text=f"🚀 Session: {current_path.name}",
            )
            thread_ts = resp["ts"]

        window_id = await create_session_for_thread(
            user_id, thread_ts, str(current_path)
        )
        if window_id:
            await client.chat_update(
                channel=channel, ts=message_ts,
                text=f"✅ Session created: {current_path.name}",
                blocks=[],
            )
        else:
            await client.chat_update(
                channel=channel, ts=message_ts,
                text="❌ Failed to create session",
                blocks=[],
            )

    elif action_id == ACTION_DIR_CANCEL:
        clear_browse_state(user_id)
        await client.chat_update(
            channel=channel, ts=message_ts,
            text="Cancelled.", blocks=[],
        )


@app.action(re.compile(r"^nav_"))
async def handle_nav_action(ack, body, client):
    """Handle interactive UI navigation button clicks."""
    await ack()
    action = body["actions"][0]
    action_id = action["action_id"]
    # Format: nav_{key}_{window_id}
    parts = action_id.replace(ACTION_NAV_PREFIX, "").rsplit("_", 1)
    if len(parts) != 2:
        return
    key_name, window_id = parts

    key_map = {
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "enter": "Enter", "esc": "Escape", "space": "Space", "tab": "Tab",
    }
    tmux_key = key_map.get(key_name)
    if tmux_key:
        from ....tmux_manager import tmux_manager
        await tmux_manager.send_special_key(window_id, tmux_key)

    if key_name == "refresh":
        # Re-capture and update the UI
        user_id = body["user"]["id"]
        channel = body["channel"]["id"]
        thread_ts = body["message"].get("thread_ts", "")
        from .handlers.interactive_ui import handle_interactive_ui
        pane_text = await tmux_manager.capture_pane(window_id, last_n_lines=20)
        if pane_text:
            await handle_interactive_ui(
                client, user_id, thread_ts, window_id, channel, pane_text
            )
```

Also add `import re` and the `from pathlib import Path` at the top of bot.py.

Update the `handle_message` event to trigger directory browser for top-level messages:

```python
@app.event("message")
async def handle_message(event, say, client):
    if _is_bot_message(event):
        return
    user_id = event.get("user", "")
    if not config.is_user_allowed(user_id):
        return

    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts")
    text = event.get("text", "")

    if not thread_ts:
        # Top-level message → show directory browser
        browser = build_directory_browser(user_id)
        await client.chat_postMessage(
            channel=channel,
            text=browser["text"],
            blocks=browser["blocks"],
        )
        return

    # Thread message → forward to Claude
    window_id = session_manager.resolve_window_for_thread(user_id, thread_ts)
    if not window_id:
        await say(
            text="This thread is not bound to a session.",
            channel=channel,
            thread_ts=thread_ts,
        )
        return

    success, msg = await session_manager.send_to_window(window_id, text)
    if not success:
        await say(text=f"Failed: {msg}", channel=channel, thread_ts=thread_ts)
```

**Step 2: Verify syntax**

Run: `uv run ruff check src/ccbot/transports/slack/`

**Step 3: Commit**

```bash
git add src/ccbot/transports/slack/bot.py
git commit -m "feat: wire up block_actions for directory browser and navigation"
```

---

### Task 15: Wire up status polling + interactive UI detection in Slack bot

**Files:**
- Modify: `src/ccbot/transports/slack/bot.py`

**Step 1: Start status polling as background task**

In `run_slack_bot()`, add the status polling background task:

```python
from .handlers.status_polling import start_status_polling

async def _run() -> None:
    await session_manager.resolve_stale_ids()

    monitor = SessionMonitor()
    monitor.set_message_callback(handle_new_message)
    monitor.start()

    # Start status polling
    asyncio.create_task(start_status_polling(app.client, _resolve_dm_channel))

    logger.info("Starting Slack bot in Socket Mode...")
    handler = AsyncSocketModeHandler(app, config.slack_app_token)
    await handler.start_async()
```

**Step 2: Verify full bot syntax**

Run: `uv run ruff check src/ccbot/transports/slack/ && uv run pyright src/ccbot/transports/slack/`

**Step 3: Commit**

```bash
git add src/ccbot/transports/slack/
git commit -m "feat: integrate status polling into Slack bot lifecycle"
```

---

### Task 16: Slack cleanup handler

**Files:**
- Create: `src/ccbot/transports/slack/handlers/cleanup.py`

**Step 1: Implement cleanup**

```python
# src/ccbot/transports/slack/handlers/cleanup.py
"""State cleanup for Slack transport.

Cleans up thread bindings, status messages, and interactive UI state
when a session is terminated.
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
    """Clean up all state for a thread binding.

    Optionally kills the associated tmux window.
    """
    window_id = session_manager.unbind_thread(user_id, thread_ts)
    if not window_id:
        return

    await clear_interactive_msg(client, user_id, thread_ts, channel)
    await clear_status(user_id, thread_ts, client, channel)

    if kill_window:
        await tmux_manager.kill_window(window_id)
        logger.info("Killed window %s for thread %s", window_id, thread_ts)
```

**Step 2: Verify syntax**

Run: `uv run ruff check src/ccbot/transports/slack/handlers/cleanup.py`

**Step 3: Commit**

```bash
git add src/ccbot/transports/slack/handlers/cleanup.py
git commit -m "feat: add Slack cleanup handler"
```

---

### Task 17: End-to-end integration test

**Files:**
- Modify: `src/ccbot/transports/slack/bot.py` (final adjustments)

**Step 1: Manual integration test**

1. Create a Slack App at https://api.slack.com/apps
2. Enable Socket Mode, get `xapp-` token
3. Add Bot Token Scopes: `chat:write`, `im:history`, `im:read`, `im:write`, `files:write`, `users:read`
4. Install to workspace, get `xoxb-` token
5. Subscribe to bot events: `message.im`
6. Enable interactivity (for block_actions)
7. Add to `.env`:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   ALLOWED_USERS=U0123ABC
   ```
8. Run: `uv run ccbot --transport slack`
9. DM the bot → should see directory browser
10. Select a directory → should create session thread
11. Send text in thread → should forward to Claude
12. Claude responses should appear in thread

**Step 2: Fix any issues found during testing**

**Step 3: Run linting and type checks**

Run: `uv run ruff check src/ && uv run ruff format src/ --check && uv run pyright src/ccbot/`

**Step 4: Final commit**

```bash
git add -A
git commit -m "feat: Slack transport integration complete"
```

---

## Task Summary

| Task | Description | Estimated Size |
|------|-------------|---------------|
| 1 | Add slack-bolt dependency | XS |
| 2 | Create transports package structure | XS |
| 3 | Move Telegram code into transports/telegram/ | L (file moves + import updates) |
| 4 | Update config.py for transport selection | M |
| 5 | Update main.py with --transport flag | S |
| 6 | Update session.py for string thread IDs | M (ripple to Telegram callers) |
| 7 | Slack formatter (mrkdwn) | S |
| 8 | Slack message splitter | S |
| 9 | Slack message sender | S |
| 10 | Slack bot core (Socket Mode) | M |
| 11 | Slack status polling | M |
| 12 | Slack directory browser (Block Kit) | M |
| 13 | Slack interactive UI (Block Kit) | M |
| 14 | Wire up block_actions | M |
| 15 | Wire up status polling | S |
| 16 | Slack cleanup handler | S |
| 17 | End-to-end integration test | M |

**Critical path:** Tasks 1-6 (infrastructure) → Tasks 7-9 (Slack utilities) → Tasks 10-16 (Slack handlers) → Task 17 (integration)

Tasks 7, 8, 9 can run in parallel. Tasks 11, 12, 13 can run in parallel after Task 10.
