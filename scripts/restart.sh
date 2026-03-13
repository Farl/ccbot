#!/usr/bin/env bash
set -euo pipefail

TMUX_SESSION="ccbot"
TMUX_WINDOW="__main__"
TARGET="${TMUX_SESSION}:${TMUX_WINDOW}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MAX_WAIT=10  # seconds to wait for process to exit

# Ensure uv is on PATH (handles tmux sessions without full shell init)
export PATH="$HOME/.local/bin:$PATH"

# Ensure tmux session and window exist, creating them if needed
if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "tmux session '$TMUX_SESSION' not found, creating..."
    tmux new-session -d -s "$TMUX_SESSION" -n "$TMUX_WINDOW"
elif ! tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$TMUX_WINDOW"; then
    echo "Window '$TMUX_WINDOW' not found, creating..."
    tmux new-window -t "$TMUX_SESSION" -n "$TMUX_WINDOW"
fi

# Get the pane PID and check if uv run ccbot is running
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
        # Kill the uv process directly
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

# Start ccbot
echo "Starting ccbot in $TARGET..."
tmux send-keys -t "$TARGET" "cd ${PROJECT_DIR} && PATH=\"\$HOME/.local/bin:\$PATH\" uv run ccbot" Enter

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
