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

**Out of scope:** Window picker (`build_window_picker`) — Telegram shows this when an unbound message arrives and existing tmux windows exist. Slack sessions are DM-thread–bound; the directory browser is shown instead. This is a conscious omission.

---

## Priority 1 — Critical Fixes

### 1.1 Read Offset Tracking

**Problem:** `handle_new_message` in `slack/bot.py` never calls `update_user_window_offset`. On bot restart, all messages in a session are re-delivered from the beginning.

**Fix:** After delivering each complete message (for each `user_id, wid, thread_id` in the results of `find_users_for_session`), resolve the session file path and update the offset:

```python
session = await session_manager.resolve_session_for_window(wid)
if session and session.file_path:
    try:
        file_size = Path(session.file_path).stat().st_size
        session_manager.update_user_window_offset(user_id, wid, file_size)
    except OSError:
        pass
```

The `wid` is already available in the `find_users_for_session` loop — no additional lookup required.

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

**Channel resolution:** The `channel` (DM channel ID) is obtained at enqueue time from the event payload — it is already known in `handle_new_message` via `_resolve_dm_channel(user_id)`. Pass `channel` as a field of `MessageTask`. The queue worker does not need to resolve it independently.

**Queue behaviour (identical to Telegram):**
- FIFO per user — guarantees message ordering
- Message merging — consecutive mergeable content messages merged on dequeue (3800 char limit)
- tool_use/tool_result pairing — record tool_use message `ts` in `_tool_msg_ids`; when tool_result arrives, call `chat.update` to edit in-place. The editing logic lives in the **queue worker**, not the formatter.
- Status-to-content conversion — first content message edits the existing status message instead of sending a new one

**Status message coordination:** The queue worker's status tracking uses the same key `(user_id, thread_ts)` as `status_polling._status_msgs`. To avoid conflicts, the queue worker must read the current status message `ts` from `status_polling._status_msgs` when doing the status-to-content conversion, then clear it. The status polling module must expose a `take_status_ts(user_id, thread_ts) -> str | None` helper that returns and removes the entry atomically.

**Queue shutdown:** `run_slack_bot()` must call `shutdown_workers()` (from `message_queue`) in its teardown path, mirroring Telegram's `post_shutdown`.

**Files:** `src/ccbot/transports/slack/handlers/message_queue.py`

---

### 1.3 Content-Type Formatting

Add a pure `build_response_parts(text, is_complete, content_type, role) -> list[str]` function (no API calls) in `slack/handlers/message_queue.py`. The queue worker calls this to get formatted parts, then handles API calls (send/edit) separately.

| content_type | Slack rendering |
|---|---|
| `text` | Plain mrkdwn |
| `thinking` | `> _thinking..._` (blockquote) |
| `tool_use` | Tool name header + parameters |
| `tool_result` | (handled by worker: edits original tool_use message `ts`) |
| `user` echo | `👤` prefix |

**Important:** The existing `handle_new_message` in `slack/bot.py` applies `👤` prefix inline (`text = f"👤 {msg.text}" if msg.role == "user" else msg.text`). Once the queue is wired in, this line must be removed from `bot.py` — the prefix will be applied by `build_response_parts` instead, to avoid double-prefixing.

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

**Block Kit format:** One button per session, displaying first 8 chars of session_id + cwd + last-modified time. Action IDs: `sess_select_{idx}`, `sess_new`, `sess_cancel`.

**In-memory state:** Store the `ClaudeSession` list in a module-level dict keyed by `(user_id, msg_ts)` — same pattern as `_browse_states`. The `sess_select_{idx}` action callback uses this to resolve `sessions[idx]`. State must be cleared on `sess_cancel`, `sess_new`, and after a successful `sess_select`.

**Resume path (`sess_select`):**
1. Look up the chosen `ClaudeSession` from state
2. Call `tmux_manager.create_window(cwd)` to get a fresh window
3. Send `claude --resume <session_id>` to the window via `send_keys`
4. Wait for `session_map` entry via `wait_for_session_map_entry`
5. **Override `window_states` to track the original `session_id`** — `--resume` causes the hook to report a new session_id, but the JSONL messages continue writing to the original file. See `topic-architecture.md`: "the bot overrides window_state to track the original session_id."
6. Bind the thread to the window via `session_manager.bind_thread`

**Files:** `src/ccbot/transports/slack/handlers/directory_browser.py`
**Bot wiring:** Add `@slack_app.action(re.compile(r"^sess_"))` handler in `slack/bot.py`
**Cleanup:** `cleanup_thread` in `cleanup.py` must also clear session picker state if present.

---

## Priority 3 — Commands

### 3.1 Command Handler (`slack/handlers/commands.py`)

Unified command dispatch supporting both text prefix (`!cmd`) and Slack slash commands (`/cmd`).

**Parsing:**
- Message starts with `!` → intercepted in `handle_message` event handler before forwarding to Claude → calls `dispatch_command`
- Slack slash command → `@app.command("/esc")` etc. registered in `slack/bot.py` → calls `dispatch_command` after `ack()`

**Bound-window guard:** All commands that operate on a session must first call `session_manager.resolve_window_for_thread(user_id, thread_ts)`. If no window is bound, reply with an appropriate error message (e.g. `"No active session in this thread."`) and return early.

**Supported commands:**

| Command | Action |
|---|---|
| `/esc` / `!esc` | Send Escape key to the thread's bound tmux window |
| `/unbind` / `!unbind` | Call `cleanup_thread` from `cleanup.py` — do not re-implement the steps |
| `/screenshot` / `!screenshot` | See Priority 4 |
| `/history [page]` / `!history [page]` | See Priority 4 |

**Files:** `src/ccbot/transports/slack/handlers/commands.py`

---

## Priority 4 — Rich Features

### 4.1 Screenshot (`/screenshot` / `!screenshot`)

1. `tmux_manager.capture_pane(window_id)` — get terminal text
2. `screenshot.text_to_image(text)` — render to PNG bytes (existing `screenshot.py`, function is `text_to_image`, not `render_png`)
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

**Action ID format:** `hist_{direction}_{page}_{window_id}` where direction is `prev` or `next`. The handler splits on `_` with a known fixed structure (4 parts). Example: `hist_prev_2_@5`.

**Files:**
- `src/ccbot/transports/slack/handlers/history.py`
- `src/ccbot/transports/slack/handlers/commands.py` (dispatch)
- Bot wiring: `@slack_app.action(re.compile(r"^hist_"))` in `slack/bot.py`

---

### 4.3 Image Handling

1. `file_shared` event with `mimetype` starting with `image/`
2. Get download URL from `files.info` response (`url_private_download` field)
3. HTTP GET with `Authorization: Bearer <bot_token>` header using `httpx` (already a dep via `transcribe.py`)
4. Save to temp path
5. Forward path to Claude via tmux (same pattern as `photo_handler` in Telegram)

**Files:** `src/ccbot/transports/slack/bot.py` (add `file_shared` event handler)

---

### 4.4 Voice Transcription

1. `file_shared` event with audio mimetype (`audio/*`)
2. Download audio file using `url_private_download` + httpx (same as image download pattern)
3. Pass bytes to `transcribe.transcribe_voice(ogg_data)` (existing `transcribe.py`, function is `transcribe_voice`, not `transcribe_audio`)
4. Send transcription text through normal message flow (same as a user text message)

**Files:** `src/ccbot/transports/slack/bot.py` (add to `file_shared` event handler)

---

## File Change Summary

| File | Change |
|---|---|
| `slack/bot.py` | Add offset tracking, remove inline 👤 prefix, wire message queue, add `file_shared` handler, add slash command registrations, add `sess_` and `hist_` action handlers, add queue shutdown in teardown |
| `slack/handlers/message_queue.py` | New — FIFO queue, merging, tool pairing, `build_response_parts`, queue shutdown |
| `slack/handlers/status_polling.py` | Add `take_status_ts(user_id, thread_ts)` helper for queue coordination |
| `slack/handlers/directory_browser.py` | Add session picker (`build_session_picker`, `_session_picker_states`, `create_session_for_thread` resume path with session_id override) |
| `slack/handlers/cleanup.py` | Clear session picker state and tool_msg_ids entries on thread cleanup |
| `slack/handlers/commands.py` | New — unified `!cmd` + slash command dispatch |
| `slack/handlers/history.py` | New — paginated history with Block Kit navigation |

## Testing Strategy

- Unit tests for `message_queue.py`: queue ordering, merging, tool_use/tool_result pairing, status-to-content conversion
- Unit tests for `commands.py`: command parsing for both `!` prefix and slash; bound-window guard behaviour
- Unit tests for `history.py`: pagination, page boundary conditions, action ID parsing
- Unit tests for session picker state machine: store → button click → resume path
- Existing formatter/splitter tests continue to pass
- Manual integration: run `ccbot run --transport slack` and verify each feature end-to-end
