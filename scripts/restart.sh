#!/usr/bin/env bash
set -euo pipefail

# Restart ccbot in its tmux window.
# Usage:
#   ./scripts/restart.sh                  # Restart Telegram (default)
#   ./scripts/restart.sh slack            # Restart Slack transport
#   ./scripts/restart.sh telegram         # Restart Telegram transport explicitly

TRANSPORT="${1:-telegram}"
TMUX_SESSION="ccbot"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MAX_WAIT=10  # seconds to wait for process to exit

# Resolve uv binary (absolute path avoids tmux PATH issues)
UV_BIN="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"
if [[ ! -x "$UV_BIN" ]]; then
    echo "Error: uv not found. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Determine tmux window name based on transport
if [[ "$TRANSPORT" == "slack" ]]; then
    TMUX_WINDOW="__slack__"
else
    TMUX_WINDOW="__main__"
fi
TARGET="${TMUX_SESSION}:${TMUX_WINDOW}"

# Ensure tmux session exists
if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "tmux session '$TMUX_SESSION' not found, creating..."
    tmux new-session -d -s "$TMUX_SESSION" -n "$TMUX_WINDOW"
elif ! tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$TMUX_WINDOW"; then
    echo "Window '$TMUX_WINDOW' not found, creating..."
    tmux new-window -t "$TMUX_SESSION" -n "$TMUX_WINDOW"
fi

# Get the pane PID and check if ccbot is running
PANE_PID=$(tmux list-panes -t "$TARGET" -F '#{pane_pid}')

is_ccbot_running() {
    pstree -a "$PANE_PID" 2>/dev/null | grep -q 'uv.*run ccbot\|ccbot.*\.venv/bin/ccbot'
}

# Stop existing process if running
if is_ccbot_running; then
    echo "Found running ccbot process, sending Ctrl-C..."
    tmux send-keys -t "$TARGET" C-c

    # Wait for process to exit
    waited=0
    while is_ccbot_running && [ "$waited" -lt "$MAX_WAIT" ]; do
        sleep 1
        waited=$((waited + 1))
        echo "  Waiting for process to exit... (${waited}s/${MAX_WAIT}s)"
    done

    if is_ccbot_running; then
        echo "Process did not exit after ${MAX_WAIT}s, sending SIGTERM..."
        UV_PID=$(pstree -ap "$PANE_PID" 2>/dev/null | grep -oP 'uv,\K\d+' | head -1)
        if [ -n "$UV_PID" ]; then
            kill "$UV_PID" 2>/dev/null || true
            sleep 2
        fi
        if is_ccbot_running; then
            echo "Process still running, sending SIGKILL..."
            kill -9 "$UV_PID" 2>/dev/null || true
            sleep 1
        fi
    fi

    echo "Process stopped."
else
    echo "No ccbot process running in $TARGET"
fi

# Brief pause to let the shell settle
sleep 1

# Build the run command with absolute paths
RUN_CMD="cd ${PROJECT_DIR} && ${UV_BIN} run ccbot"
if [[ "$TRANSPORT" == "slack" ]]; then
    RUN_CMD="$RUN_CMD --transport slack"
fi

# Start ccbot
echo "Starting ccbot ($TRANSPORT) in $TARGET..."
tmux send-keys -t "$TARGET" "$RUN_CMD" Enter

# Verify startup and show logs
sleep 3
if is_ccbot_running; then
    echo "ccbot restarted successfully. Recent logs:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -20
    echo "----------------------------------------"
else
    echo "Warning: ccbot may not have started. Pane output:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -30
    echo "----------------------------------------"
    exit 1
fi
