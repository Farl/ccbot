# Slack Transport Design

## Goal

Add Slack App as an alternative transport alongside Telegram. Each Slack App DM thread maps to one tmux window (one Claude Code session), mirroring the existing Telegram topic model.

## Why keep both

- Preserve Telegram code for upstream merge compatibility
- Telegram code serves as reference for Slack implementation

## Architecture

### Directory Structure

```
src/ccbot/
├── main.py                  # CLI: --transport slack|telegram
├── config.py                # Add SLACK_BOT_TOKEN, SLACK_APP_TOKEN
├── session.py               # thread_id changed to str type
├── terminal_parser.py       # Unchanged
├── tmux_manager.py          # Unchanged
├── session_monitor.py       # Callback injection from transport
├── transcript_parser.py     # Unchanged
├── hook.py                  # Unchanged
├── screenshot.py            # Unchanged
├── monitor_state.py         # Unchanged
├── utils.py                 # Unchanged
│
├── transports/
│   ├── __init__.py
│   ├── telegram/
│   │   ├── __init__.py
│   │   ├── bot.py           # Existing bot.py moved here
│   │   ├── handlers/        # Existing handlers/ moved here
│   │   ├── markdown_v2.py
│   │   └── telegram_sender.py
│   └── slack/
│       ├── __init__.py
│       ├── bot.py           # Socket Mode App + event listeners
│       ├── handlers/
│       │   ├── message_queue.py
│       │   ├── interactive_ui.py
│       │   ├── directory_browser.py
│       │   ├── status_polling.py
│       │   ├── message_sender.py
│       │   └── cleanup.py
│       ├── formatter.py     # MD -> Slack mrkdwn
│       └── splitter.py      # 4000 char split
```

### Core Mapping

| Telegram | Slack | Notes |
|----------|-------|-------|
| `chat_id` | `channel_id` | App DM channel |
| `message_thread_id` (topic) | `thread_ts` | Parent message timestamp |
| `user_id` | `user_id` | Slack user ID |
| `callback_query` | `block_actions` | Button interactions |
| `edit_message_text` | `chat.update` | In-place edit |
| `delete_message` | `chat.delete` | Remove message |
| `InlineKeyboardButton` | Block Kit `button` | Action blocks |
| MarkdownV2 | mrkdwn | Different syntax |
| 4096 char limit | 4000 char limit | Split at send layer |

### Startup

```bash
ccbot run --transport slack      # Start Slack version
ccbot run --transport telegram   # Start Telegram version (default)
```

## Slack App Configuration

### Dependencies

- `slack-bolt` — Official Slack Python framework with Socket Mode
- `slack-sdk` — Underlying API client (bundled with bolt)

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_BOT_TOKEN` | Yes | `xoxb-` Bot User OAuth Token |
| `SLACK_APP_TOKEN` | Yes | `xapp-` Socket Mode token |
| `ALLOWED_USERS` | Yes | Comma-separated Slack user IDs |

### Required OAuth Scopes

- `chat:write` — Send messages
- `im:history` — Read DM messages
- `im:read` — Read DM channel info
- `im:write` — Open DM channels
- `files:write` — Upload file snippets (long message fallback)
- `users:read` — Read user info

### Socket Mode Events

- `message` (im channel) — User sends text
- `block_actions` — Block Kit button clicks
- `file_shared` — User sends image/file

## Thread Model

- New session: bot sends a top-level message in DM (e.g. "New session: project-name")
- The message's `ts` becomes the `thread_ts` key for all subsequent interactions
- All messages in the session go into this thread
- `thread_bindings`: `user_id -> {thread_ts -> window_id}`

## First Version Scope

### Included (Phase 1)

- Text message send/receive (thread = session)
- Interactive UI via Block Kit buttons (permission prompts, AskUserQuestion, etc.)
- Status polling (show Claude working status)
- Directory browser (select directory to create new session)

### Deferred (Phase 2)

- /history paginated message history
- /screenshot terminal capture
- Voice transcription
- Photo/image forwarding
- /esc, /kill, /unbind management commands
- Message merging (consecutive message batching)
- Tool use <-> tool result in-place editing

## Key Changes to Shared Modules

### session.py

- `thread_bindings` value type: `dict[int, str]` -> `dict[str, str]`
  - Telegram: int thread_id cast to str
  - Slack: thread_ts stored as-is

### session_monitor.py

- `handle_new_message` callback injected by each transport at startup
- No direct Telegram/Slack imports

### config.py

- Add `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` validation
- Transport selection via `--transport` CLI arg

### main.py

- Route to `transports.telegram.bot.run()` or `transports.slack.bot.run()`

## Telegram Side Changes

Minimal — move files into `transports/telegram/`, update import paths. No logic changes.
