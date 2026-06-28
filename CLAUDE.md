# CLAUDE.md

ccmux — Bot that bridges Telegram/Slack to Claude Code sessions via tmux windows. Each thread/topic is bound to one tmux window running one Claude Code instance.

Tech stack: Python, python-telegram-bot, slack-bolt, tmux, uv.

## Common Commands

```bash
uv run ccbot                          # Start Telegram transport (default)
uv run ccbot --transport slack        # Start Slack transport
uv run ruff check src/ tests/         # Lint — MUST pass before committing
uv run ruff format src/ tests/        # Format — auto-fix, then verify with --check
uv run pyright src/ccbot/             # Type check — MUST be 0 errors before committing
./scripts/restart.sh                  # Restart the ccbot service after code changes
ccbot hook --install                  # Auto-install Claude Code SessionStart hook
```

Note: Telegram and Slack transports run as **separate processes**. To run both simultaneously, start each in its own tmux window.

## Core Design Constraints

- **1 Thread = 1 Window = 1 Session** — all internal routing keyed by tmux window ID (`@0`, `@12`), not window name. Window names kept as display names. Same directory can have multiple windows.
- **Shared session layer** — `SessionManager` stores both Telegram (numeric user IDs) and Slack (string user IDs like `U...`) bindings in the same `state.json`. Transport-specific code must guard against the other transport's ID format (e.g. Telegram's `int()` calls skip non-numeric IDs).
- **No message truncation** at parse layer — splitting only at send layer (4096 char for Telegram, 3000 char for Slack).
- **Hook-based session tracking** — `SessionStart` hook writes `session_map.json`; monitor polls it to detect session changes.
- **Message queue per user** — FIFO ordering, message merging (3800 char limit), tool_use/tool_result pairing.

### Telegram-specific
- **MarkdownV2 only** — use `safe_reply`/`safe_edit`/`safe_send` helpers (auto fallback to plain text).
- **Rate limiting** — `AIORateLimiter(max_retries=5)` on the Application (30/s global). On restart, the global bucket is pre-filled to avoid burst against Telegram's server-side counter.
- **Topic-only** — no backward-compat for non-topic mode.

### Slack-specific
- **Assistants framework** — `assistant.user_message` intercepts messages in assistant threads before `@slack_app.event("message")`. Both handlers must implement the same features (files, text commands).
- **Block Kit** — interactive UI uses Slack Block Kit (buttons, sections, actions).

## Code Conventions

- Never expose user privacy, secret, keys, ... etc.
- Code as Document. When a decision was shaped by a non-obvious lesson (bug, review feedback, edge case), leave a short comment explaining *why* — so the next person doesn't undo it.
- Never hardcode.
- Every `.py` file starts with a module-level docstring: purpose clear within 10 lines, one-sentence summary first line, then core responsibilities and key components.
- Telegram interaction: prefer inline keyboards over reply keyboards; use `edit_message_text` for in-place updates; keep callback data under 64 bytes; use `answer_callback_query` for instant feedback.

## Configuration

- Config directory: `~/.ccbot/` by default, override with `CCBOT_DIR` env var.
- `.env` loading priority: local `.env` > config dir `.env`.
- State files: `state.json` (thread bindings), `session_map.json` (hook-generated), `monitor_state.json` (byte offsets).

## Hook Configuration

Auto-install: `ccbot hook --install`

Or manually in `~/.claude/settings.json`:
```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }]
      }
    ]
  }
}
```

## Upstream & Fork Maintenance

- This repo (`Farl/ccbot`) is a **private fork** of **`six-ddc/ccbot`** (upstream).
  Private use — we do **not** contribute changes back upstream.
- The `upstream` remote is not configured by default. Add it with:
  `git remote add upstream git@github.com:six-ddc/ccbot.git`
- **Always scope `gh` to the fork: `gh pr create --repo Farl/ccbot ...`** (and
  `gh pr merge/list/view --repo Farl/ccbot`). Because this is a GitHub fork, a
  bare `gh pr create` defaults to the **upstream** `six-ddc/ccbot` and would
  open a public PR against it — which both fails confusingly ("No commits
  between main and ...") and violates "do not contribute back upstream". Same
  trap for `gh pr list`, which otherwise lists upstream's PRs, not ours.
- **Branches:** `main` is the **single maintained branch** — the unified Telegram +
  Slack bot, and where ccbot runs locally. Sync upstream by integrating `upstream/main`
  into `main` (merge is the established pattern). History: the Slack transport was
  developed on `feat/slack-transport-universal` and merged via PR #1 on 2026-06-28; once
  the two branches were byte-identical we consolidated to main-only (2026-06-28) to drop
  the reconcile overhead. The `origin/feat/slack-transport-universal` ref is kept for
  history but no longer maintained; the local branch was deleted.
- **Topic/thread auto-titling is a Slack-side concern, not Telegram.** Slack assistant
  threads have no user-given name, so Slack titles them via `_set_thread_title`
  (`assistant_threads_setTitle`) in `transports/slack/bot.py` — keep that. Telegram topics
  are user-named, so we follow upstream PR #73 (`350c653`): do **not** rename topics on
  bind. The Telegram silent/active icon still updates on explicit `/silent` toggle.
- **Notification filters:** granular flags (`show_thinking`/`show_tool_use`/
  `show_tool_result`/`show_user_messages`/`show_status`, gated in `session_monitor.py` for
  both transports) coexist with upstream's `CCBOT_SHOW_TOOL_CALLS`, which is treated as a
  master switch for tool_use+tool_result; `config.show_tool_calls` is derived for the
  queue-worker gate in `bot.py`.
- **Running inside a Claude Code session** (e.g. developing ccbot from ccbot): set
  `CLAUDE_COMMAND=env -u CLAUDECODE claude` in `.env`, or child `claude` processes fail with
  "cannot be launched inside another Claude Code session". tmux is a hard prerequisite.

## Architecture Details

See @.claude/rules/architecture.md for full system diagram and module inventory.
See @.claude/rules/topic-architecture.md for topic→window→session mapping details.
See @.claude/rules/message-handling.md for message queue, merging, and rate limiting.
