#!/usr/bin/env python3
"""nudgling — a tiny desktop pet that nudges you when Claude Code is done.

It listens for Claude Code hook events (sent from any machine, e.g. a box you
SSH into, over Tailscale/LAN) and shows them as a floating widget with a little
blob whose mood follows your sessions:

    sleepy  (- -)  nothing going on
    busy    (o o)  Claude is working
    happy   (^ ^)  Claude finished
    alarmed (O O)  Claude needs your input

Done / needs-input events also fire a desktop notification + sound.

Controls: drag to move · click a session row to dismiss it · right-click to quit.

Config (env vars or ~/.config/nudgling/config.env):
    NUDGLING_TOKEN   shared secret (required, must match the hook side)
    NUDGLING_PORT    default 8765
    NUDGLING_BIND    default 0.0.0.0 (set to your Tailscale IP to restrict)
    NUDGLING_SOUND   1/0, default 1
"""
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "0.1.0"


def load_config():
    cfg = {}
    path = os.path.expanduser("~/.config/nudgling/config.env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    cfg.update({k: v for k, v in os.environ.items() if k.startswith("NUDGLING_")})
    return cfg


CFG = load_config()
PORT = int(CFG.get("NUDGLING_PORT", "8765"))
BIND = CFG.get("NUDGLING_BIND", "0.0.0.0")
TOKEN = CFG.get("NUDGLING_TOKEN", "")
SOUND = CFG.get("NUDGLING_SOUND", "1") != "0"

# palette
BG, PANEL, FG, MUTED = "#1e1e2e", "#28283d", "#cdd6f4", "#6c7086"
BLOB = {"idle": "#b4befe", "working": "#89b4fa", "done": "#a6e3a1", "attention": "#fab387"}
STATUS = {
    "working":   ("✦", "working"),
    "done":      ("✔", "done!"),
    "attention": ("!", "needs you"),
}
LINES = {
    "idle": "zzz…",
    "working": "thinking hard…",
    "done": "all done!",
    "attention": "psst! over here",
}

events: "queue.Queue[dict]" = queue.Queue()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not TOKEN or self.headers.get("X-Nudgling-Token") != TOKEN:
            self.send_response(403); self.end_headers(); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            events.put(json.loads(self.rfile.read(min(n, 65536))))
            self.send_response(204)
        except Exception:
            self.send_response(400)
        self.end_headers()

    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"nudgling is awake\n")

    def log_message(self, *a):
        pass


SOUNDS = [
    "/usr/share/sounds/freedesktop/stereo/complete.oga",
    "/usr/share/sounds/freedesktop/stereo/message.oga",
]


def notify(title, body, urgent=False):
    if shutil.which("notify-send"):
        cmd = ["notify-send", "-a", "nudgling", "-u", "critical" if urgent else "normal", title, body]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if SOUND and shutil.which("paplay"):
        snd = next((s for s in SOUNDS if os.path.exists(s)), None)
        if snd:
            subprocess.Popen(["paplay", snd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Pet:
    """The little blob, drawn on a canvas."""
    W, H = 96, 78

    def __init__(self, parent):
        self.c = tk.Canvas(parent, width=self.W, height=self.H, bg=BG, highlightthickness=0)
        self.mood = "idle"
        self.t = 0.0
        self.blink = 0

    def set_mood(self, mood):
        self.mood = mood

    def draw(self):
        c, m = self.c, self.mood
        c.delete("all")
        self.t += 0.12
        cx = self.W / 2
        # bounce: gentle when idle, bouncy when happy, jittery when alarmed
        if m == "done":
            dy = -abs(math.sin(self.t * 2.2)) * 8
        elif m == "attention":
            dy = math.sin(self.t * 9) * 1.5
        elif m == "working":
            dy = math.sin(self.t * 3) * 2.5
        else:
            dy = math.sin(self.t * 1.2) * 1.5
        base = self.H - 10
        squish = 1 + (0.06 * math.sin(self.t * 2.2) if m == "done" else 0.03 * math.sin(self.t * 1.2))
        w, h = 34 * squish, 30 / squish
        top = base - 2 * h + dy

        # shadow
        c.create_oval(cx - 26, base - 3, cx + 26, base + 5, fill="#181825", outline="")
        # body
        col = BLOB[m]
        c.create_oval(cx - w, top, cx + w, base + dy, fill=col, outline="")
        # little antenna / sprout
        sx = cx + 6
        c.create_line(sx, top + 3, sx + 5, top - 9, fill=col, width=3, smooth=True)
        c.create_oval(sx + 1, top - 15, sx + 10, top - 6,
                      fill="#f9e2af" if m == "attention" else col, outline="")
        # cheeks
        ey = top + h * 0.85
        c.create_oval(cx - 25, ey + 6, cx - 15, ey + 12, fill="#f5c2e7", outline="")
        c.create_oval(cx + 15, ey + 6, cx + 25, ey + 12, fill="#f5c2e7", outline="")
        # eyes
        ink = "#1e1e2e"
        self.blink = (self.blink + 1) % 70
        blinking = self.blink > 66 and m in ("idle", "working")
        for ex in (cx - 11, cx + 11):
            if m == "idle" or blinking:
                c.create_line(ex - 4, ey, ex + 4, ey, fill=ink, width=2)
            elif m == "done":
                c.create_line(ex - 5, ey + 2, ex, ey - 3, ex + 5, ey + 2, fill=ink, width=2.5)
            elif m == "attention":
                c.create_oval(ex - 5, ey - 5, ex + 5, ey + 5, fill="white", outline=ink, width=2)
                c.create_oval(ex - 2, ey - 2, ex + 2, ey + 2, fill=ink, outline="")
            else:  # working: eyes darting
                off = math.sin(self.t * 2) * 2
                c.create_oval(ex - 3 + off, ey - 3, ex + 3 + off, ey + 3, fill=ink, outline="")
        # mouth
        my = ey + 9
        if m == "done":
            c.create_arc(cx - 6, my - 6, cx + 6, my + 5, start=200, extent=140,
                         style="arc", outline=ink, width=2)
        elif m == "attention":
            c.create_oval(cx - 3, my - 2, cx + 3, my + 4, fill=ink, outline="")
        elif m == "working":
            c.create_line(cx - 3, my, cx + 3, my, fill=ink, width=2)
        else:
            c.create_arc(cx - 3, my - 3, cx + 3, my + 2, start=200, extent=140,
                         style="arc", outline=ink, width=1.5)
        # mood props
        if m == "idle":
            z = (self.t * 8) % 24
            c.create_text(cx + 30, top - z / 2 + 4, text="z", fill=MUTED, font=("Sans", 8 + int(z / 8)))
        elif m == "working":
            for i in range(3):
                a = self.t * 2.5 + i * 2.09
                c.create_text(cx + math.cos(a) * 40, top + 16 + math.sin(a) * 10,
                              text="·", fill=col, font=("Sans", 14, "bold"))
        elif m == "done":
            for i, (px, py) in enumerate(((-38, 10), (36, 4), (30, 34))):
                if int(self.t * 3 + i) % 3:
                    c.create_text(cx + px, top + py, text="✦", fill="#f9e2af", font=("Sans", 9))
        elif m == "attention":
            c.create_text(cx + 34, top + 4, text="!", fill=col, font=("Sans", 14, "bold"))


class Widget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("nudgling")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.95)
        except tk.TclError:
            pass
        self.root.configure(bg=BG)
        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"+{sw - 300}+40")

        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=10, pady=(8, 2))
        self.pet = Pet(top)
        self.pet.c.pack(side="left")
        info = tk.Frame(top, bg=BG)
        info.pack(side="left", fill="both", expand=True, padx=(6, 0))
        tk.Label(info, text="nudgling", bg=BG, fg=FG, font=("Sans", 11, "bold"),
                 anchor="w").pack(fill="x", pady=(14, 0))
        self.say = tk.Label(info, text=LINES["idle"], bg=BG, fg=MUTED,
                            font=("Sans", 9, "italic"), anchor="w")
        self.say.pack(fill="x")

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", padx=8, pady=(0, 8))
        self.empty = tk.Label(self.body, text="no sessions yet", bg=BG, fg=MUTED,
                              font=("Sans", 8), pady=2)
        self.empty.pack()

        self.sessions = {}
        for w in (self.root, top, self.pet.c, info, self.say):
            w.bind("<Button-1>", self._start_drag)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<Button-3>", lambda e: self.root.destroy())
        self._animate()
        self.root.after(200, self._poll)
        self.root.after(1000, self._tick)

    # dragging
    def _start_drag(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    # loops
    def _animate(self):
        self.pet.draw()
        self.root.after(50, self._animate)

    def _poll(self):
        while not events.empty():
            self._handle(events.get())
        self.root.after(200, self._poll)

    def _tick(self):
        for s in self.sessions.values():
            self._render(s)
        self.root.after(1000, self._tick)

    def _update_mood(self):
        states = [s["state"] for s in self.sessions.values()]
        if "attention" in states:
            mood = "attention"
        elif any(s["state"] == "done" and time.time() - s["ts"] < 120
                 for s in self.sessions.values()):
            mood = "done"
        elif "working" in states:
            mood = "working"
        else:
            mood = "idle"
        if mood != self.pet.mood:
            self.pet.set_mood(mood)
            self.say.config(text=LINES[mood], fg=BLOB[mood] if mood != "idle" else MUTED)

    # events
    def _handle(self, ev):
        state = ev.get("state", "done")
        if state not in STATUS:
            return
        key = ev.get("session_id") or ev.get("cwd", "?")
        proj = os.path.basename((ev.get("cwd") or "").rstrip("/")) or "~"
        where = ev.get("tmux") or ev.get("host", "")
        label = f"{proj} · {where}" if where else proj

        s = self.sessions.get(key)
        if s is None:
            f = tk.Frame(self.body, bg=PANEL, padx=6, pady=3)
            icon = tk.Label(f, bg=PANEL, font=("Sans", 10, "bold"), width=2)
            text = tk.Label(f, bg=PANEL, fg=FG, font=("Sans", 9), anchor="w")
            status = tk.Label(f, bg=PANEL, font=("Sans", 8), anchor="e")
            icon.pack(side="left"); text.pack(side="left", fill="x", expand=True)
            status.pack(side="right", padx=(10, 0))
            s = {"frame": f, "icon": icon, "text": text, "status": status}
            for w in (f, icon, text, status):
                w.bind("<Button-1>", lambda e, k=key: self._dismiss(k))
                w.bind("<Button-3>", lambda e: self.root.destroy())
            self.sessions[key] = s
            self.empty.pack_forget()
            f.pack(fill="x", pady=2)
        s.update(state=state, label=label, ts=time.time())
        self._render(s)
        self._update_mood()

        if state == "done":
            notify("Claude is done ✔", label)
            self._flash(s)
        elif state == "attention":
            notify("Claude needs you!", f"{label}\n{ev.get('message', '')}".strip(), urgent=True)
            self._flash(s)

    def _render(self, s):
        glyph, word = STATUS[s["state"]]
        color = BLOB[s["state"]]
        age = int(time.time() - s["ts"])
        ago = f"{age}s" if age < 60 else f"{age // 60}m" if age < 3600 else f"{age // 3600}h"
        s["icon"].config(text=glyph, fg=color)
        s["text"].config(text=s["label"])
        s["status"].config(text=f"{word} · {ago}", fg=color)
        self._update_mood()

    def _flash(self, s, n=6):
        if n <= 0 or not s["frame"].winfo_exists():
            return
        bg = "#3b3b58" if n % 2 == 0 else PANEL
        for w in (s["frame"], s["icon"], s["text"], s["status"]):
            w.config(bg=bg)
        self.root.after(280, self._flash, s, n - 1)

    def _dismiss(self, key):
        s = self.sessions.pop(key, None)
        if s:
            s["frame"].destroy()
        if not self.sessions:
            self.empty.pack()
        self._update_mood()

    def run(self):
        self.root.mainloop()


def main():
    if "--version" in sys.argv:
        print(__version__); return
    if not TOKEN:
        sys.exit("nudgling: set NUDGLING_TOKEN (env or ~/.config/nudgling/config.env)")
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"nudgling listening on {BIND}:{PORT}")
    if "--demo" in sys.argv:
        def demo():
            for st, sid, cwd in [("working", "a", "~/webapp"), ("working", "b", "~/api"),
                                 ("done", "a", "~/webapp"), ("attention", "b", "~/api")]:
                time.sleep(3)
                events.put({"state": st, "session_id": sid, "cwd": cwd, "tmux": f"dev:{sid}",
                            "message": "Claude needs your permission to use Bash"})
        threading.Thread(target=demo, daemon=True).start()
    Widget().run()


if __name__ == "__main__":
    main()
