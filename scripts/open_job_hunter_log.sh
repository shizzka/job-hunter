#!/usr/bin/env bash
set -euo pipefail

readonly TMUX_SOCKET="jobhunter-monitor"
readonly TMUX_SESSION="live"
readonly LOG_FILE="/home/q/.job-hunter/profiles/qa/job-hunter.log"

if ! /usr/bin/tmux -L "$TMUX_SOCKET" has-session -t "$TMUX_SESSION" 2>/dev/null; then
    /usr/bin/tmux -L "$TMUX_SOCKET" -f /dev/null new-session -d -s "$TMUX_SESSION" -n log "exec /usr/bin/tail -n 1000 -F $LOG_FILE"
fi

/usr/bin/tmux -L "$TMUX_SOCKET" set-option -g history-limit 1000
exec /usr/bin/tmux -L "$TMUX_SOCKET" attach-session -t "$TMUX_SESSION"
