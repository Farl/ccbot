---
name: restart-ccbot
description: Restart the ccbot service. Arguments: `$ARGUMENTS` (optional: `telegram`, `slack`, or `all`; defaults to `slack`).
---

# Restart ccbot

Run the restart script via WSL. The script handles: stopping the running process, creating the tmux window if needed, and starting ccbot with the correct transport flag.

**IMPORTANT:** Always run commands via WSL: `wsl -e bash -lc "..."`. The script must be run through `bash` directly (not `./scripts/restart.sh`) to avoid Windows line ending issues.

## Steps

1. Parse the transport argument from `$ARGUMENTS`:
   - Empty or `slack` → restart Slack only
   - `telegram` → restart Telegram only
   - `all` → restart both (Telegram first, then Slack)

2. For each transport, run (convert CRLF first since Windows may save with `\r`):
   ```
   wsl -e bash -lc "cd '$(wslpath -u "$(pwd)")' && sed -i 's/\r$//' scripts/restart.sh && bash scripts/restart.sh <transport>"
   ```
   Use a 30-second timeout.

3. If the script exits with code 0, report success. If it fails, show the output for debugging.

4. When restarting `all`, run Telegram first, then Slack sequentially (they use different tmux windows: `__main__` for Telegram, `__slack__` for Slack).
