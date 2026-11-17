#!/bin/sh
# Runs `agenthub hook EVENT` with the hook's JSON on stdin. The agenthub package next to the plugin (a checkout of
# the repository) wins, then `agenthub` on PATH, then the one `agenthub install` writes (Claude Code started from a
# desktop launcher may not have ~/.local/bin on PATH); without any the hook does nothing, so it never blocks work.
here=$(dirname "$0")
root=$(cd "$here/../.." 2>/dev/null && pwd)
if [ -n "$root" ] && [ -f "$root/agenthub/__main__.py" ]; then
    PYTHONPATH="$root" exec python3 -P -m agenthub hook "$1"
fi
if command -v agenthub >/dev/null 2>&1; then
    exec agenthub hook "$1"
fi
if [ -x "$HOME/.local/bin/agenthub" ]; then
    exec "$HOME/.local/bin/agenthub" hook "$1"
fi
exit 0
