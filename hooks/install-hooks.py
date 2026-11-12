#!/usr/bin/env python3
"""Install the nudgling hook into Claude Code on this (remote) machine.

    python3 install-hooks.py --url http://100.x.y.z:8765/event --token SECRET

Copies nudgling-hook.sh to ~/.config/nudgling/, writes hook.env, and merges
hook entries into Claude Code's settings.json (respects $CLAUDE_CONFIG_DIR).
Safe to re-run; existing settings and other hooks are preserved.
"""
import argparse
import json
import os
import shutil
import stat

EVENTS = {"UserPromptSubmit": "working", "Stop": "done", "Notification": "attention"}

ap = argparse.ArgumentParser()
ap.add_argument("--url", required=True, help="widget URL, e.g. http://100.x.y.z:8765/event")
ap.add_argument("--token", required=True, help="shared secret (same as the widget)")
args = ap.parse_args()

cfg_dir = os.path.expanduser("~/.config/nudgling")
os.makedirs(cfg_dir, exist_ok=True)
hook = os.path.join(cfg_dir, "nudgling-hook.sh")
shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "nudgling-hook.sh"), hook)
os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)
env_path = os.path.join(cfg_dir, "hook.env")
with open(env_path, "w") as f:
    f.write(f'NUDGLING_URL="{args.url}"\nNUDGLING_TOKEN="{args.token}"\n')
os.chmod(env_path, 0o600)

claude_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
os.makedirs(claude_dir, exist_ok=True)
settings_path = os.path.join(claude_dir, "settings.json")
settings = {}
if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
    shutil.copy(settings_path, settings_path + ".bak")

hooks = settings.setdefault("hooks", {})
for event, state in EVENTS.items():
    cmd = f"{hook} {state}"
    groups = hooks.setdefault(event, [])
    # drop any previous nudgling entries, then add ours
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if "nudgling-hook.sh" not in h.get("command", "")]
    groups[:] = [g for g in groups if g.get("hooks")]
    groups.append({"hooks": [{"type": "command", "command": cmd}]})

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
print(f"nudgling hooks installed into {settings_path}")
print("restart running Claude Code sessions to pick them up.")
