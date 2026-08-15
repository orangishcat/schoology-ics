#!/bin/bash
# --- settings you can tweak ---
APPDIR="$(pwd)"
PY="$APPDIR/.venv/bin/python"
SCRIPT="$APPDIR/src/main.py"
NAME="sCal"

source "$APPDIR/.env"
export DEBUG=0

# Kill any existing instance
pkill -x $NAME

cd "$APPDIR" || exit 1

# Run in background, silence output, detach from Automator
nohup "$PY" "$SCRIPT" "$NAME" >/dev/null 2>&1 &

# Let Automator exit immediately (no window lingers)
exit 0
