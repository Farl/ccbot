# Slack Thread Title via `setTitle`

## Problem

In Telegram, the bot renames forum topics to show which directory a session is bound to. Slack assistant threads have no equivalent visual indicator — users must open each thread to find out which session it belongs to.

## Solution

Call `assistant.threads.setTitle` after session creation/resume to set the thread title to the working directory path (with `~` abbreviation).

## Implementation

### Touch points

All in `src/ccbot/transports/slack/bot.py`:

1. `handle_dir_action` — `ACTION_DIR_CONFIRM` branch (new session, no existing sessions)
2. `handle_sess_action` — `ACTION_SESS_SELECT` branch (resume existing session)
3. `handle_sess_action` — `ACTION_SESS_NEW` branch (new session from session picker)

### Code pattern

After each successful `chat_update` confirming session creation:

**Touch point 1** (`handle_dir_action` — `ACTION_DIR_CONFIRM`): uses `current_path` (Path). Must be placed after the `if not thread_ts` block so `effective_ts` reflects the actual thread root.

```python
display_path = str(current_path).replace(str(Path.home()), "~")
try:
    await client.assistant_threads_setTitle(
        channel_id=channel, thread_ts=effective_ts, title=display_path
    )
except Exception:
    logger.debug("Failed to set thread title", exc_info=True)
```

**Touch points 2 & 3** (`handle_sess_action`): uses `cwd` (str), not `current_path`.

```python
display_path = cwd.replace(str(Path.home()), "~")
try:
    await client.assistant_threads_setTitle(
        channel_id=channel, thread_ts=thread_ts, title=display_path
    )
except Exception:
    logger.debug("Failed to set thread title", exc_info=True)
```

### Title format

Full path with `~` home abbreviation: `~/Documents/Prototyper/ccbot`

### Error handling

Silent failure with debug log. Thread title is cosmetic — must not interrupt the session creation flow.

### Scope boundaries

- Set title once at session bind time (matches Telegram's topic rename behavior)
- No reverse sync (Slack does not expose thread title change events)
- Only affects assistant threads (`setTitle` is a no-op for regular threads)
- No new modules, helpers, or configuration options
