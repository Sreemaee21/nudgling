#!/usr/bin/env python3
"""nudgling — a tiny desktop pet that nudges you when Claude Code is done.

It listens for Claude Code hook events (sent from any machine, e.g. a box you
SSH into, over Tailscale/LAN) and shows them as a floating widget with a little
pet whose mood follows your sessions:

    sleepy   nothing going on
    busy     Claude is working
    happy    Claude finished
    alarmed  Claude needs your input

Done / needs-input events also fire a desktop notification + sound.

Pets: blob (default), cat, ghost, robot, oswald (an octopus). Pick one with --pet NAME,
NUDGLING_PET=NAME, or the right-click menu (your choice is remembered).

Controls: drag to move · click a session row to dismiss it · right-click for menu.

Config (env vars or ~/.config/nudgling/config.env):
    NUDGLING_TOKEN   shared secret (required, must match the hook side)
    NUDGLING_PORT    default 8765
    NUDGLING_BIND    default 0.0.0.0 (set to your Tailscale IP to restrict)
    NUDGLING_SOUND   1/0, default 1
    NUDGLING_PET     blob | cat | ghost | robot | oswald

Other commands:
    nudgling --demo            run through all moods with fake sessions
    nudgling --export DIR      write SVG previews of every pet (no Tk needed)
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "0.3.0"

CONFIG_DIR = os.path.expanduser("~/.config/nudgling")


def load_config():
    cfg = {}
    path = os.path.join(CONFIG_DIR, "config.env")
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
BG, PANEL, FG, MUTED, INK = "#1e1e2e", "#28283d", "#cdd6f4", "#6c7086", "#1e1e2e"
SHADOW, BLUSH, GOLD = "#181825", "#f5c2e7", "#f9e2af"
MOOD_COLOR = {"idle": "#b4befe", "working": "#89b4fa", "done": "#a6e3a1", "attention": "#fab387"}
MOODS = ("idle", "working", "done", "attention")
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


# ───────────────────────────── drawing backends ─────────────────────────────
# Pets are drawn through a tiny painter interface so the same code renders
# on a Tk canvas (the live widget) and to SVG (README screenshots).

class TkPainter:
    def __init__(self, canvas):
        self.c = canvas

    def clear(self):
        self.c.delete("all")

    def oval(self, x0, y0, x1, y1, fill="", outline="", width=1):
        self.c.create_oval(x0, y0, x1, y1, fill=fill, outline=outline, width=width)

    def line(self, pts, fill, width=2, smooth=False):
        self.c.create_line(*pts, fill=fill, width=width, smooth=smooth,
                           capstyle="round", joinstyle="round")

    def arc(self, x0, y0, x1, y1, start, extent, outline, width=2):
        self.c.create_arc(x0, y0, x1, y1, start=start, extent=extent,
                          style="arc", outline=outline, width=width)

    def poly(self, pts, fill, outline="", width=1, smooth=False):
        self.c.create_polygon(*pts, fill=fill, outline=outline, width=width, smooth=smooth)

    def text(self, x, y, s, fill, size=9, bold=False):
        self.c.create_text(x, y, text=s, fill=fill,
                           font=("Sans", size, "bold" if bold else "normal"))


def _pairs(pts):
    return [(pts[i], pts[i + 1]) for i in range(0, len(pts), 2)]


def _mid(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


class SvgPainter:
    """Mimics Tk's canvas semantics (incl. its quadratic-spline smoothing)."""

    def __init__(self):
        self.out = []
        self.dx = self.dy = 0

    def clear(self):
        pass

    def _p(self, x, y):
        return f"{_f(x + self.dx)},{_f(y + self.dy)}"

    def oval(self, x0, y0, x1, y1, fill="", outline="", width=1):
        cx, cy = (x0 + x1) / 2 + self.dx, (y0 + y1) / 2 + self.dy
        stroke = f' stroke="{outline}" stroke-width="{width}"' if outline else ""
        self.out.append(f'<ellipse cx="{_f(cx)}" cy="{_f(cy)}" rx="{_f(abs(x1 - x0) / 2)}" '
                        f'ry="{_f(abs(y1 - y0) / 2)}" fill="{fill or "none"}"{stroke}/>')

    def _smooth_path(self, pts, closed):
        P = _pairs(pts)
        if closed:
            d = f"M{self._p(*_mid(P[-1], P[0]))}"
            for i, p in enumerate(P):
                d += f" Q{self._p(*p)} {self._p(*_mid(p, P[(i + 1) % len(P)]))}"
            return d + " Z"
        if len(P) < 3:
            return "M" + " L".join(self._p(*p) for p in P)
        d = f"M{self._p(*P[0])}"
        for i in range(1, len(P) - 1):
            end = P[i + 1] if i == len(P) - 2 else _mid(P[i], P[i + 1])
            d += f" Q{self._p(*P[i])} {self._p(*end)}"
        return d

    def line(self, pts, fill, width=2, smooth=False):
        d = self._smooth_path(pts, False) if smooth else \
            "M" + " L".join(self._p(*p) for p in _pairs(pts))
        self.out.append(f'<path d="{d}" fill="none" stroke="{fill}" stroke-width="{width}" '
                        f'stroke-linecap="round" stroke-linejoin="round"/>')

    def arc(self, x0, y0, x1, y1, start, extent, outline, width=2):
        cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) / 2, abs(y1 - y0) / 2
        a0, a1 = math.radians(start), math.radians(start + extent)
        p0 = (cx + rx * math.cos(a0), cy - ry * math.sin(a0))
        p1 = (cx + rx * math.cos(a1), cy - ry * math.sin(a1))
        large = 1 if abs(extent) > 180 else 0
        sweep = 0 if extent > 0 else 1
        self.out.append(f'<path d="M{self._p(*p0)} A{_f(rx)},{_f(ry)} 0 {large} {sweep} '
                        f'{self._p(*p1)}" fill="none" stroke="{outline}" stroke-width="{width}" '
                        f'stroke-linecap="round"/>')

    def poly(self, pts, fill, outline="", width=1, smooth=False):
        d = self._smooth_path(pts, True) if smooth else \
            "M" + " L".join(self._p(*p) for p in _pairs(pts)) + " Z"
        stroke = f' stroke="{outline}" stroke-width="{width}"' if outline else ""
        self.out.append(f'<path d="{d}" fill="{fill}"{stroke}/>')

    def text(self, x, y, s, fill, size=9, bold=False, anchor="middle", italic=False):
        s = s.replace("&", "&amp;").replace("<", "&lt;")
        w = ' font-weight="bold"' if bold else ""
        it = ' font-style="italic"' if italic else ""
        # Tk font sizes are points; at 96dpi that is ~1.33px
        self.out.append(f'<text x="{_f(x + self.dx)}" y="{_f(y + self.dy)}" fill="{fill}" '
                        f'font-family="DejaVu Sans, Verdana, sans-serif" font-size="{_f(size * 1.33)}"'
                        f'{w}{it} text-anchor="{anchor}" dominant-baseline="central">{s}</text>')


# ──────────────────────────────────── pets ───────────────────────────────────

PET_W, PET_H = 96, 92


def rounded_rect(x0, y0, x1, y1, r):
    """Point list for a smooth=True polygon that renders as a rounded rectangle."""
    return [x0 + r, y0, x0 + r, y0, x1 - r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y0 + r,
            x1, y1 - r, x1, y1 - r, x1, y1, x1 - r, y1, x1 - r, y1, x0 + r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y1 - r, x0, y0 + r, x0, y0 + r, x0, y0]


def bounce(mood, t):
    if mood == "done":
        return -abs(math.sin(t * 2.2)) * 7
    if mood == "attention":
        return math.sin(t * 9) * 1.5
    if mood == "working":
        return math.sin(t * 3) * 2.5
    return math.sin(t * 1.2) * 1.5


def face(p, cx, ey, mood, t, blinking, ink=INK, gap=11, cheeks=True):
    if cheeks:
        p.oval(cx - gap - 14, ey + 6, cx - gap - 4, ey + 12, fill=BLUSH)
        p.oval(cx + gap + 4, ey + 6, cx + gap + 14, ey + 12, fill=BLUSH)
    for ex in (cx - gap, cx + gap):
        if mood == "idle" or blinking:
            p.line([ex - 4, ey, ex + 4, ey], fill=ink, width=2)
        elif mood == "done":
            p.line([ex - 5, ey + 2, ex, ey - 3, ex + 5, ey + 2], fill=ink, width=2.5)
        elif mood == "attention":
            p.oval(ex - 5, ey - 5, ex + 5, ey + 5, fill="white", outline=ink, width=2)
            p.oval(ex - 2, ey - 2, ex + 2, ey + 2, fill=ink)
        else:  # working: eyes darting side to side
            off = math.sin(t * 2) * 2
            p.oval(ex - 3 + off, ey - 3, ex + 3 + off, ey + 3, fill=ink)
    my = ey + 9
    if mood == "done":
        p.arc(cx - 6, my - 6, cx + 6, my + 5, 200, 140, outline=ink, width=2)
    elif mood == "attention":
        p.oval(cx - 3, my - 2, cx + 3, my + 4, fill=ink)
    elif mood == "working":
        p.line([cx - 3, my, cx + 3, my], fill=ink, width=2)
    else:
        p.arc(cx - 3, my - 3, cx + 3, my + 2, 200, 140, outline=ink, width=1.5)


def props(p, cx, top, mood, t, col, bang=1):
    if mood == "idle":
        z = (t * 8) % 24
        p.text(cx + 32, top - z / 2 + 4, "z", fill=MUTED, size=8 + int(z / 8))
    elif mood == "working":
        for i in range(3):
            a = t * 2.5 + i * 2.09
            p.text(cx + math.cos(a) * 40, top + 16 + math.sin(a) * 10, "·",
                   fill=col, size=14, bold=True)
    elif mood == "done":
        for i, (px, py) in enumerate(((-38, 10), (36, 4), (30, 34))):
            if int(t * 3 + i) % 3:
                p.text(cx + px, top + py, "✦", fill=GOLD, size=9)
    elif mood == "attention":
        p.text(cx + 36 * bang, top + 4, "!", fill=col, size=14, bold=True)


def draw_blob(p, mood, t, blinking):
    cx, base, col = PET_W / 2, PET_H - 10, MOOD_COLOR[mood]
    dy = bounce(mood, t)
    sq = 1 + (0.06 * math.sin(t * 2.2) if mood == "done" else 0.03 * math.sin(t * 1.2))
    w, h = 34 * sq, 30 / sq
    top = base - 2 * h + dy
    p.oval(cx - 26, base - 3, cx + 26, base + 5, fill=SHADOW)
    p.oval(cx - w, top, cx + w, base + dy, fill=col)
    sx = cx + 6  # sprout
    p.line([sx, top + 3, sx + 5, top - 9], fill=col, width=3)
    p.oval(sx + 1, top - 15, sx + 10, top - 6, fill=GOLD if mood == "attention" else col)
    face(p, cx, top + h * 0.85, mood, t, blinking)
    props(p, cx, top, mood, t, col)


def draw_cat(p, mood, t, blinking):
    cx, base, col = PET_W / 2 - 4, PET_H - 10, MOOD_COLOR[mood]
    dy = bounce(mood, t) * 0.8
    w, h = 30, 26
    top = base - 2 * h + dy
    p.oval(cx - 26, base - 3, cx + 30, base + 5, fill=SHADOW)
    # tail (wags faster when happy)
    wag = math.sin(t * (6 if mood == "done" else 1.5)) * (7 if mood == "done" else 3)
    p.line([cx + 24, base - 6 + dy, cx + 40, base - 14 + dy, cx + 38 + wag, top + 8],
           fill=col, width=6, smooth=True)
    # ears
    for s in (-1, 1):
        p.poly([cx + s * 26, top + 16, cx + s * 22, top - 8, cx + s * 6, top + 4], fill=col)
        p.poly([cx + s * 21, top + 10, cx + s * 20, top - 1, cx + s * 11, top + 5], fill=BLUSH)
    p.oval(cx - w, top, cx + w, base + dy, fill=col)
    ey = top + h * 0.8
    face(p, cx, ey, mood, t, blinking, gap=10)
    for s in (-1, 1):  # whiskers
        p.line([cx + s * 16, ey + 9, cx + s * 30, ey + 6], fill=INK, width=1)
        p.line([cx + s * 16, ey + 12, cx + s * 30, ey + 13], fill=INK, width=1)
    props(p, cx + 2, top, mood, t, col, bang=-1)


def draw_ghost(p, mood, t, blinking):
    cx, col = PET_W / 2, MOOD_COLOR[mood]
    float_y = math.sin(t * 1.6) * 3 + bounce(mood, t) * 0.7
    y0 = PET_H - 66 + float_y
    bottom = y0 + 50
    L, R = cx - 26, cx + 26
    sh = 18 - float_y  # shadow shrinks as it floats higher
    p.oval(cx - sh, PET_H - 7, cx + sh, PET_H - 1, fill=SHADOW)
    pts = [L, bottom, L, y0 + 18, L + 3, y0, cx, y0 - 8, R - 3, y0, R, y0 + 18, R, bottom]
    n = 4  # wavy hem
    for i in range(1, n * 2):
        x = R - (R - L) * i / (n * 2)
        wave = 7 if i % 2 else 0
        pts += [x, bottom - wave + math.sin(t * 3 + i) * 1.5]
    p.poly(pts, fill=col, smooth=True)
    face(p, cx, y0 + 20, mood, t, blinking, gap=10)
    props(p, cx, y0, mood, t, col)


def draw_robot(p, mood, t, blinking):
    cx, base, col = PET_W / 2, PET_H - 10, MOOD_COLOR[mood]
    dy = bounce(mood, t) * 0.6
    top, bot = base - 48 + dy, base + dy
    p.oval(cx - 26, base - 3, cx + 26, base + 5, fill=SHADOW)
    # antenna
    p.line([cx, top, cx, top - 9], fill=col, width=2)
    lit = mood == "attention" or (mood == "working" and int(t * 4) % 2)
    p.oval(cx - 4, top - 15, cx + 4, top - 7, fill=GOLD if lit else col)
    # ears / bolts
    for s in (-1, 1):
        p.poly(rounded_rect(cx + s * 26 - 4, top + 16, cx + s * 26 + 4, top + 32, 3),
               fill=col, smooth=True)
    p.poly(rounded_rect(cx - 26, top, cx + 26, bot, 10), fill=col, smooth=True)
    # screen face with glowing features
    p.poly(rounded_rect(cx - 19, top + 7, cx + 19, top + 37, 7), fill=INK, smooth=True)
    face(p, cx, top + 18, mood, t, blinking, ink=col, gap=9, cheeks=False)
    props(p, cx, top, mood, t, col)


def lighten(hex_color, f):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(int(c + (255 - c) * f) for c in (r, g, b))


def draw_oswald(p, mood, t, blinking):
    """Oswald the octopus: his tentacles show how busy Claude is."""
    cx, base, col = PET_W / 2, PET_H - 10, MOOD_COLOR[mood]
    dy = bounce(mood, t) * 0.6
    top = base - 60 + dy
    amp, speed = {"idle": (1, 1), "working": (3, 6), "done": (2, 4), "attention": (3, 10)}[mood]
    p.oval(cx - 28, base - 3, cx + 28, base + 5, fill=SHADOW)
    for i in range(5):
        k = i - 2
        rx, ry = cx + k * 8, top + 38
        d = 1 if k >= 0 else -1
        wig = math.sin(t * speed + i * 1.3) * amp
        # waving arms: both up to cheer when done, one up to say "hey!" when he needs you
        raised = (mood == "done" and abs(k) == 2) or (mood == "attention" and k == -2)
        if raised:
            lift = math.sin(t * speed) * 4
            pts = [rx, ry, rx + d * 16, ry - 4, rx + d * 22 + wig, top + 10 + lift,
                   rx + d * 16 + wig, top + 4 + lift]
        elif mood == "idle":  # limp, sprawled out
            pts = [rx, ry, rx + k * 6, base - 2, rx + k * 11 + wig, base,
                   rx + k * 11 + d * 5, base - 4]
        else:
            ex = cx + k * 15
            pts = [rx, ry, (rx + ex) / 2 + wig, base - 10, ex - wig, base - 1,
                   ex + d * 6, base - 6 + wig * 0.5]
        p.line(pts, fill=col, width=6, smooth=True)
    # head
    p.oval(cx - 25, top, cx + 25, top + 46, fill=col)
    spot = lighten(col, 0.35)
    for sx, sy, r in ((-12, 7, 3.5), (-4, 4, 2), (11, 6, 2.5)):
        p.oval(cx + sx - r, top + sy - r, cx + sx + r, top + sy + r, fill=spot)
    face(p, cx, top + 26, mood, t, blinking, gap=10)
    props(p, cx, top, mood, t, col, bang=1)


PETS = {"blob": draw_blob, "cat": draw_cat, "ghost": draw_ghost, "robot": draw_robot,
        "oswald": draw_oswald}


def draw_pet(p, pet, mood, t, blinking=False):
    PETS.get(pet, draw_blob)(p, mood, t, blinking)


# ────────────────────────────────── export ───────────────────────────────────

# hand-picked animation times that make each mood read well in a still
STILL_T = {"idle": 0.4, "working": 0.3, "done": 0.75, "attention": 0.0}


def _svg(w, h, body, radius=14):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}"><rect width="{w}" height="{h}" rx="{radius}" fill="{BG}"/>'
            + "".join(body) + "</svg>\n")


def export(outdir):
    os.makedirs(outdir, exist_ok=True)
    # 1) gallery: every pet in every mood
    cw, ch, lw, top = 110, 100, 64, 26
    p = SvgPainter()
    for j, mood in enumerate(MOODS):
        p.dx, p.dy = 0, 0
        p.text(lw + j * cw + cw / 2, 18, {"idle": "sleepy", "working": "working",
               "done": "done", "attention": "needs you"}[mood],
               fill=MOOD_COLOR[mood], size=9, bold=True)
    for i, pet in enumerate(PETS):
        p.dx, p.dy = 0, 0
        p.text(12, top + i * ch + PET_H / 2, pet, fill=FG, size=9, bold=True, anchor="start")
        for j, mood in enumerate(MOODS):
            p.dx, p.dy = lw + j * cw + (cw - PET_W) / 2, top + i * ch
            draw_pet(p, pet, mood, STILL_T[mood])
    with open(os.path.join(outdir, "pets.svg"), "w") as f:
        f.write(_svg(lw + cw * 4 + 6, top + ch * len(PETS) + 4, p.out))

    # 2) a tiny mock of the widget itself
    w, h = 280, 160
    p = SvgPainter()
    p.dx, p.dy = 10, 0
    draw_pet(p, "blob", "done", STILL_T["done"])
    p.dx = p.dy = 0
    p.text(116, 40, "nudgling", fill=FG, size=11, bold=True, anchor="start")
    p.text(116, 58, "all done!", fill=MOOD_COLOR["done"], size=9, anchor="start", italic=True)
    rows = [("✔", "webapp · dev:1", "done! · 4s", "done"),
            ("!", "api · dev:2", "needs you · 1m", "attention")]
    for k, (g, label, st, mood) in enumerate(rows):
        y = 104 + k * 26
        p.out.append(f'<rect x="8" y="{y - 11}" width="{w - 16}" height="22" rx="5" fill="{PANEL}"/>')
        p.text(22, y, g, fill=MOOD_COLOR[mood], size=10, bold=True)
        p.text(36, y, label, fill=FG, size=9, anchor="start")
        p.text(w - 16, y, st, fill=MOOD_COLOR[mood], size=8, anchor="end")
    with open(os.path.join(outdir, "widget.svg"), "w") as f:
        f.write(_svg(w, h, p.out))
    print(f"wrote {outdir}/pets.svg and {outdir}/widget.svg")


# ─────────────────────────────── server + widget ─────────────────────────────

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


PET_FILE = os.path.join(CONFIG_DIR, "pet")


def saved_pet():
    try:
        with open(PET_FILE) as f:
            name = f.read().strip()
            return name if name in PETS else None
    except OSError:
        return None


def save_pet(name):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(PET_FILE, "w") as f:
            f.write(name + "\n")
    except OSError:
        pass


class Widget:
    def __init__(self, pet):
        import tkinter as tk
        self.tk = tk
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

        self.pet = tk.StringVar(value=pet)
        self.mood, self.t, self.blink = "idle", 0.0, 0

        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=10, pady=(8, 2))
        self.canvas = tk.Canvas(top, width=PET_W, height=PET_H, bg=BG, highlightthickness=0)
        self.canvas.pack(side="left")
        self.painter = TkPainter(self.canvas)
        info = tk.Frame(top, bg=BG)
        info.pack(side="left", fill="both", expand=True, padx=(6, 0))
        tk.Label(info, text="nudgling", bg=BG, fg=FG, font=("Sans", 11, "bold"),
                 anchor="w").pack(fill="x", pady=(24, 0))
        self.say = tk.Label(info, text=LINES["idle"], bg=BG, fg=MUTED,
                            font=("Sans", 9, "italic"), anchor="w")
        self.say.pack(fill="x")

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", padx=8, pady=(0, 8))
        self.empty = tk.Label(self.body, text="no sessions yet", bg=BG, fg=MUTED,
                              font=("Sans", 8), pady=2)
        self.empty.pack()

        self.menu = tk.Menu(self.root, tearoff=0, bg=PANEL, fg=FG,
                            activebackground="#3b3b58", activeforeground=FG)
        pets = tk.Menu(self.menu, tearoff=0, bg=PANEL, fg=FG,
                       activebackground="#3b3b58", activeforeground=FG)
        for name in PETS:
            pets.add_radiobutton(label=name, variable=self.pet, value=name,
                                 command=lambda n=name: save_pet(n))
        self.menu.add_cascade(label="Pet", menu=pets)
        self.menu.add_command(label="Clear sessions", command=self._clear)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.root.destroy)

        self.sessions = {}
        for w in (self.root, top, self.canvas, info, self.say, self.empty):
            w.bind("<Button-1>", self._start_drag)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<Button-3>", self._popup)
        self._animate()
        self.root.after(200, self._poll)
        self.root.after(1000, self._tick)

    def _popup(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    # dragging
    def _start_drag(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    # loops
    def _animate(self):
        self.t += 0.12
        self.blink = (self.blink + 1) % 70
        self.painter.clear()
        draw_pet(self.painter, self.pet.get(), self.mood, self.t,
                 blinking=self.blink > 66 and self.mood in ("idle", "working"))
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
        if mood != self.mood:
            self.mood = mood
            self.say.config(text=LINES[mood], fg=MOOD_COLOR[mood] if mood != "idle" else MUTED)

    # events
    def _handle(self, ev):
        tk = self.tk
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
                w.bind("<Button-3>", self._popup)
            self.sessions[key] = s
            self.empty.pack_forget()
            f.pack(fill="x", pady=2)
        s.update(state=state, label=label, ts=time.time())
        self._render(s)

        if state == "done":
            notify("Claude is done ✔", label)
            self._flash(s)
        elif state == "attention":
            notify("Claude needs you!", f"{label}\n{ev.get('message', '')}".strip(), urgent=True)
            self._flash(s)

    def _render(self, s):
        glyph, word = STATUS[s["state"]]
        color = MOOD_COLOR[s["state"]]
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

    def _clear(self):
        for k in list(self.sessions):
            self._dismiss(k)

    def run(self):
        self.root.mainloop()


def arg_value(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        sys.exit(f"nudgling: {flag} needs a value")
    return None


def main():
    if "--version" in sys.argv:
        print(__version__); return
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__); return
    out = arg_value("--export")
    if out:
        export(out); return

    pet = arg_value("--pet") or CFG.get("NUDGLING_PET") or saved_pet() or "blob"
    if pet not in PETS:
        sys.exit(f"nudgling: unknown pet '{pet}' (choose from: {', '.join(PETS)})")
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
    Widget(pet).run()


if __name__ == "__main__":
    main()
