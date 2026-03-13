#!/usr/bin/env bash
set -euo pipefail

TMUX_SESSION="ccbot"
TMUX_WINDOW="__main__"
TARGET="${TMUX_SESSION}:${TMUX_WINDOW}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# If session already exists, delegate to restart.sh
if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    if tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$TMUX_WINDOW"; then
        echo "Session '$TMUX_SESSION' with window '$TMUX_WINDOW' already exists — delegating to restart.sh"
        exec "$(dirname "$0")/restart.sh"
    fi
    # Session exists but no __main__ window — create it
    echo "Session '$TMUX_SESSION' exists but missing '$TMUX_WINDOW' window, creating..."
    tmux new-window -t "$TMUX_SESSION" -n "$TMUX_WINDOW" -c "$PROJECT_DIR"
else
    echo "Creating tmux session '$TMUX_SESSION'..."
    tmux new-session -d -s "$TMUX_SESSION" -n "$TMUX_WINDOW" -c "$PROJECT_DIR"
fi

echo "Starting ccbot in $TARGET..."
tmux send-keys -t "$TARGET" "cd ${PROJECT_DIR} && uv run ccbot" Enter

sleep 3

# Verify startup
PANE_PID=$(tmux list-panes -t "$TARGET" -F '#{pane_pid}')
if pstree -a "$PANE_PID" 2>/dev/null | grep -q 'uv.*run ccbot\|ccbot.*\.venv/bin/ccbot'; then
    echo "ccbot started successfully. Recent output:"
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
