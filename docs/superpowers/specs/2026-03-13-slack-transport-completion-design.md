# Slack Transport Completion Design

**Date:** 2026-03-13
**Branch:** feat/slack-transport-universal
**Approach:** Option C — Fork Telegram patterns into Slack transport, implement incrementally by priority

## Goal

Complete the Slack transport to achieve full feature parity with the Telegram transport.
The existing Phase 1 skeleton (bot core, status polling, interactive UI, directory browser, formatter, splitter) is in place.
This design covers all remaining gaps.

## Architecture Summary

All new code lives under `src/ccbot/transports/slack/`. Shared modules (`session.py`, `session_monitor.py`, `terminal_parser.py`, etc.) are unchanged. Telegram transport is unchanged.

---

## Priority 1 — Critical Fixes

### 1.1 Read Offset Tracking

**Problem:** `handle_new_message` in `slack/bot.py` never calls `update_user_window_offset`. On bot restart, all messages in a session are re-delivered from the beginning.

**Fix:** After delivering each complete message, call:
```python
session_manager.update_user_window_offset(user_id, wid, file_size)
```
Mirror the exact pattern from `transports/telegram/bot.py:handle_new_message`.

**Files:** `src/ccbot/transports/slack/bot.py`

---

### 1.2 Message Queue (`slack/handlers/message_queue.py`)

Fork of `transports/telegram/handlers/message_queue.py` with Slack API substitutions:

| Telegram | Slack |
|---|---|
| `send_with_fallback(bot, chat_id, text)` | `send_message(client, channel, text)` |
| `bot.edit_message_text(message_id, ...)` | `edit_message(client, channel, ts, ...)` |
| `send_photo(bot, ...)` | `files.getUploadURLExternal` + `completeUploadExternal` |
| `convert_markdown(text)` | `to_mrkdwn(text)` |
| `thread_id: int` | `thread_ts: str` |
| `message_id: int` | message `ts: str` |

**Queue behaviour (identical to Telegram):**
- FIFO per user — guarantees message ordering
- Message merging — consecutive mergeable content messages merged on dequeue (3800 char limit)
- tool_use/tool_result pairing — record tool_use message `ts`; when tool_result arrives, call `chat.update` to edit in-place
- Status-to-content conversion — first content message edits the existing status message instead of sending a new one

**Files:** `src/ccbot/transports/slack/handlers/message_queue.py`

---

### 1.3 Content-Type Formatting

Add `build_response_parts` equivalent for Slack (can live in `message_queue.py` or a small `response_builder.py`):

| content_type | Slack rendering |
|---|---|
| `text` | Plain mrkdwn |
| `thinking` | `> _thinking..._` (blockquote) |
| `tool_use` | Tool name header + parameters |
| `tool_result` | Edit the original tool_use message in-place |
| `user` echo | `👤` prefix |

**Files:** `src/ccbot/transports/slack/handlers/message_queue.py`

**Wire-up:** Update `handle_new_message` in `slack/bot.py` to enqueue via the new queue instead of calling `send_long_message` directly.

---

## Priority 2 — Session Management

### 2.1 Session Picker

When a directory is confirmed in the directory browser, check for existing Claude sessions before creating a new window:

```
Confirm directory
  ├─ Existing sessions found?
  │     ├─ Yes → show session picker (Block Kit button list)
  │     │         ├─ Select session → claude --resume <session_id>
  │     │         └─ New session → normal window creation
  │     └─ No → create new window directly (existing behaviour)
```

**Block Kit format:** One button per session, displaying first 8 chars of session_id + cwd + last-modified time.

**Action IDs:** `sess_select_{idx}`, `sess_new`, `sess_cancel`

**Files:** `src/ccbot/transports/slack/handlers/directory_browser.py`
**Bot wiring:** Add `@slack_app.action(re.compile(r"^sess_"))` handler in `slack/bot.py`

---

## Priority 3 — Commands

### 3.1 Command Handler (`slack/handlers/commands.py`)

Unified command dispatch supporting both text prefix (`!cmd`) and Slack slash commands (`/cmd`).

**Parsing:**
- Message starts with `!` → intercepted in `handle_message` event handler → calls `dispatch_command`
- Slack slash command → `@app.command("/esc")` etc. registered in `slack/bot.py` → calls `dispatch_command` after `ack()`

**Supported commands:**

| Command | Action |
|---|---|
| `/esc` / `!esc` | Send Escape key to the thread's bound tmux window |
| `/unbind` / `!unbind` | Kill window, unbind thread, clean up all state |
| `/screenshot` / `!screenshot` | See Priority 4 |
| `/history [page]` / `!history [page]` | See Priority 4 |

**Files:** `src/ccbot/transports/slack/handlers/commands.py`

---

## Priority 4 — Rich Features

### 4.1 Screenshot (`/screenshot` / `!screenshot`)

1. `tmux_manager.capture_pane(window_id)` — get terminal text
2. `screenshot.render_png(text)` — render to PNG bytes (existing `screenshot.py`)
3. Upload via `files.getUploadURLExternal` + `files.completeUploadExternal` (modern Slack Files API)
4. Reply with image in the current thread

**Files:** `src/ccbot/transports/slack/handlers/commands.py`

---

### 4.2 History (`/history [page]` / `!history [page]`)

Fork of `transports/telegram/handlers/history.py` + `response_builder.py`:

1. Resolve session for thread's window
2. Parse JSONL via `TranscriptParser`
3. Paginate (configurable page size, default same as Telegram)
4. Render as Block Kit message with prev/next buttons

**Action IDs:** `hist_prev_{page}_{window_id}`, `hist_next_{page}_{window_id}`

**Files:**
- `src/ccbot/transports/slack/handlers/history.py`
- `src/ccbot/transports/slack/handlers/commands.py` (dispatch)
- Bot wiring: `@slack_app.action(re.compile(r"^hist_"))` in `slack/bot.py`

---

### 4.3 Image Handling

1. `file_shared` event with `mimetype` starting with `image/`
2. Download file using bot token (`files.info` + HTTP GET with Authorization header)
3. Save to temp path
4. Forward path to Claude via tmux (same pattern as `photo_handler` in Telegram)

**Files:** `src/ccbot/transports/slack/bot.py` (add `file_shared` event handler)

---

### 4.4 Voice Transcription

1. `file_shared` event with audio mimetype (`audio/*`)
2. Download audio file using bot token
3. Pass to `transcribe.transcribe_audio()` (existing `transcribe.py`, uses OpenAI gpt-4o-transcribe)
4. Send transcription text through normal message flow (same as a user text message)

**Files:** `src/ccbot/transports/slack/bot.py` (add to `file_shared` event handler)

---

## File Change Summary

| File | Change |
|---|---|
| `slack/bot.py` | Add offset tracking, wire message queue, add `file_shared` handler, add slash command registrations, add `sess_` and `hist_` action handlers |
| `slack/handlers/message_queue.py` | New — FIFO queue, merging, tool pairing, content formatting |
| `slack/handlers/directory_browser.py` | Add session picker (`build_session_picker`, `create_session_for_thread` resume path) |
| `slack/handlers/commands.py` | New — unified `!cmd` + slash command dispatch |
| `slack/handlers/history.py` | New — paginated history with Block Kit navigation |

## Testing Strategy

- Unit tests for `message_queue.py` (queue ordering, merging, tool pairing)
- Unit tests for `commands.py` (command parsing for both `!` prefix and slash)
- Unit tests for `history.py` (pagination, page boundary conditions)
- Existing formatter/splitter tests continue to pass
- Manual integration: run `ccbot run --transport slack` and verify each feature end-to-end
