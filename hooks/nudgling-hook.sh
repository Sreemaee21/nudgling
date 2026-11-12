#!/usr/bin/env bash
# nudgling hook: forwards Claude Code hook events to your nudgling widget.
# Usage (from Claude Code settings.json hooks): nudgling-hook.sh <working|done|attention>
#
# Config (env vars or ~/.config/nudgling/hook.env):
#   NUDGLING_URL    e.g. http://100.x.y.z:8765/event  (your desktop's Tailscale/LAN IP)
#   NUDGLING_TOKEN  same shared secret as the widget
[ -f "$HOME/.config/nudgling/hook.env" ] && . "$HOME/.config/nudgling/hook.env"
[ -z "$NUDGLING_URL" ] || [ -z "$NUDGLING_TOKEN" ] && { cat >/dev/null; exit 0; }

state="$1"
input="$(cat)"

tmux_loc=""
if [ -n "$TMUX" ] && command -v tmux >/dev/null; then
  tmux_loc="$(tmux display-message -p -t "${TMUX_PANE}" '#S:#I' 2>/dev/null)"
fi

payload="$(python3 -c '
import json, sys, os
try:
    d = json.loads(sys.argv[1] or "{}")
except Exception:
    d = {}
print(json.dumps({
    "state": sys.argv[2],
    "host": os.uname().nodename,
    "tmux": sys.argv[3],
    "session_id": d.get("session_id", ""),
    "cwd": d.get("cwd", os.getcwd()),
    "message": d.get("message", ""),
}))' "$input" "$state" "$tmux_loc")"

# fire and forget: never slow Claude down, never fail the hook
( curl -s -m 3 -X POST -H "Content-Type: application/json" \
    -H "X-Nudgling-Token: $NUDGLING_TOKEN" -d "$payload" "$NUDGLING_URL" >/dev/null 2>&1 & )
exit 0
