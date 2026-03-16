---
name: restart-ccbot
description: Restart the ccbot service. Arguments: `$ARGUMENTS` (optional: `telegram`, `slack`, or `all`; defaults to `slack`).
---

# Restart ccbot

Run the restart script. The script handles: stopping the running process, creating the tmux window if needed, and starting ccbot with the correct transport flag.

## Steps

1. Parse the transport argument from `$ARGUMENTS`:
   - Empty or `slack` → restart Slack only
   - `telegram` → restart Telegram only
   - `all` → restart both (Telegram first, then Slack)

2. **Detect the environment first**, then run accordingly:

   **macOS / Linux** (no WSL needed):
   ```
   bash scripts/restart.sh <transport>
   ```

   **Windows (WSL required)** — convert CRLF first since Windows may save with `\r`:
   ```
   wsl -e bash -lc "cd '$(wslpath -u "$(pwd)")' && sed -i 's/\r$//' scripts/restart.sh && bash scripts/restart.sh <transport>"
   ```

   To detect: run `uname` or check `$OSTYPE`. If it returns `Darwin` or `Linux`, use native bash. If running on Windows (cmd/PowerShell/WSL unavailable natively), use the WSL form.

   Use a 30-second timeout.

3. If the script exits with code 0, report success. If it fails, show the output for debugging.

4. When restarting `all`, run Telegram first, then Slack sequentially (they use different tmux windows: `__main__` for Telegram, `__slack__` for Slack).
