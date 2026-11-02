#!/usr/bin/env python3
"""Tux - a tiny local Linux helper. A penguin-shaped window with a speech bubble.

Answers come from a small local model, grounded with the best matches from
GNOME Help, man pages and your own notes. The model server only runs while
you're asking things and stops after IDLE_SECS idle (and when Tux closes).
Run setup_tux.py once to build tux-art.png and index.json.
"""
import ctypes
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request

import cairo
import gi

# software rendering: ~half the RAM of GTK's GL renderer and leaves the GPU alone
os.environ.setdefault("GSK_RENDERER", "cairo")

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

# ---------------------------------------------------------------- settings
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_BIN = os.path.join(HERE, "bin", "llama-b12024", "llama-server")
MODEL = os.path.join(HERE, "models", "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf")
ART_PNG = os.path.join(HERE, "tux-art.png")
ART_JSON = os.path.join(HERE, "tux-art.json")
INDEX = os.path.join(HERE, "index.json")
NOTES = os.path.expanduser("~/Helpful Linux Commands.txt")
PORT = 8088
THREADS = 3            # leave the rest of the CPU for VMs / remote sessions
IDLE_SECS = int(os.environ.get("TUX_IDLE_SECS", 300))  # unload model after this
MAX_TOKENS = 90
HISTORY_TURNS = 2
HELP_MIN_SCORE = 18    # how sure a GNOME Help match must be before Tux uses it
# memory savers: no repacked weight copy, no prompt-cache RAM pool, small batches
SERVER_EXTRA = ["--no-repack", "--cache-ram", "0", "-b", "512", "-ub", "256"]

SYSTEM_PROMPT = (
    "You are Tux, a Linux power-user sidekick for Ubuntu desktop (GNOME, Wayland, apt, "
    "systemd, NetworkManager). Everything about using the computer counts: terminal, "
    "desktop, settings, wifi, hardware, apps. Answer in ONE short line, then a ```bash "
    "block if a command helps. For desktop settings, give the Settings path in that line. "
    "At most one 'Pro tip:' line. Never pad. Use the Reference if relevant. Warn on "
    "destructive commands. Only for clearly non-computer topics (sports, jokes, poems) "
    "reply exactly: I only know Linux stuff!"
)
FEWSHOT = [
    ("what's eating my RAM?",
     "Top memory hogs:\n```bash\nps aux --sort=-%mem | head\n```\n"
     "Pro tip: `watch -n1 free -h` for a live view."),
    ("wifi won't connect",
     "Toggle Wi-Fi in the top-right menu, or bounce the radio:\n"
     "```bash\nnmcli radio wifi off && nmcli radio wifi on\n```"),
    ("make the text bigger",
     "Settings → Accessibility → Large Text, or:\n"
     "```bash\ngsettings set org.gnome.desktop.interface text-scaling-factor 1.25\n```"),
    ("tell me a joke", "I only know Linux stuff!"),
]
# hand-picked, safe power-user moves: shown instantly for "tip"/"trick" questions
POWER_TIPS = [
    ("sudo !!", "rerun the last command with sudo"),
    ("ctrl+r", "search your command history as you type"),
    ("cd -", "jump back to the previous directory"),
    ("alt+.", "paste the last argument of the previous command"),
    ("ctrl+a / ctrl+e", "jump to the start / end of the line"),
    ("ctrl+u", "wipe everything before the cursor"),
    ("ctrl+l", "clear the screen"),
    ("ctrl+z, then bg", "push a running job into the background"),
    ("cp file{,.bak}", "instant backup copy of a file"),
    ("mkdir -p a/b/c", "create nested folders in one go"),
    ("du -sh * | sort -h", "what's eating space in this folder"),
    ("ls -lhS", "biggest files first"),
    ("find . -mmin -10", "files changed in the last 10 minutes"),
    ("grep -rn 'text' .", "search all files here, with line numbers"),
    ("ss -tulpn", "every listening port and the process behind it"),
    ("ip -br a", "all your IPs, one line per interface"),
    ("nmcli dev wifi", "nearby Wi-Fi networks and signal strength"),
    ("journalctl -xe", "recent system errors, explained"),
    ("journalctl -fu NetworkManager", "follow one service's logs live"),
    ("systemctl --failed", "list services that are broken"),
    ("watch -n1 free -h", "rerun a command every second"),
    ("tail -f file.log", "follow a log as it grows"),
    ("lsblk -f", "drives, partitions and filesystems at a glance"),
    ("history | grep ssh", "find that command you ran last week"),
    ("xdg-open .", "open the current folder in Files"),
    ("command -v foo", "is foo installed, and where"),
    ("apt list --upgradable", "updates waiting to install"),
    ("sudo apt autoremove", "clear out packages nothing needs"),
    ("pgrep -a firefox", "find a process by name"),
    ("time cmd", "measure how long a command takes"),
    ("rsync -avh --progress src/ dst/", "copy with progress, resumable"),
    ("virsh list --all", "your VMs and whether they're running"),
]
# hand-checked instant answers for the most common asks: (triggers, answer).
# Each trigger is "any of these words"; every trigger must match. {port}/{size} get filled in.
QUICK = [
    (["port"], "What's on that port:\n```bash\nsudo ss -tulpn | grep :{port}\n```"),
    (["big bigger large larger biggest huge largest", "file files"],
     "Biggest files under here:\n```bash\nfind . -type f -size +{size} -exec ls -lh {{}} + 2>/dev/null\n```\n"
     "Pro tip: `du -sh * | sort -h` ranks folders."),
    (["disk storage drive", "space full free left"],
     "Free space:\n```bash\ndf -h /\n```\nPro tip: open Disk Usage Analyzer (`baobab`) to see what's eating it."),
    (["update upgrade"],
     "Update everything:\n```bash\nsudo apt update && sudo apt full-upgrade\n```\nPro tip: `snap refresh` for snaps."),
    (["frozen freeze hung stuck respond responding"],
     "Kill it by name (check first with `pgrep -a name`):\n```bash\npkill -f appname\n```"),
    (["wifi wireless", "drop dropping disconnect disconnecting keep slow connect broken"],
     "Bounce the radio, then check the logs:\n```bash\nnmcli radio wifi off && nmcli radio wifi on\n"
     "journalctl -b -u NetworkManager | tail -30\n```"),
    (["brightness bright dim"],
     "Top-right menu → brightness slider, or Settings → Displays. Laptop Fn keys work too."),
    (["memory ram", "most hog eat eating high full usage top"],
     "Top memory hogs:\n```bash\nps aux --sort=-%mem | head\n```"),
    (["cpu processor", "most hog eat eating high 100 hot usage top"],
     "Top CPU hogs, live:\n```bash\ntop -o %CPU\n```"),
    (["ip"],
     "Local and public IP:\n```bash\nip -br a\ncurl ifconfig.me\n```"),
    (["version release", "ubuntu os"], "```bash\nlsb_release -a\n```"),
    (["kernel"], "```bash\nuname -r\n```"),
    (["deb"], "Install a .deb (pulls in its dependencies):\n```bash\nsudo apt install ./file.deb\n```"),
    (["tar tgz untar"], "Extract any tarball (gz/xz/bz2 auto-detected):\n```bash\ntar -xf file.tar.gz\n```"),
    (["unzip zip"], "```bash\nunzip file.zip -d folder/\n```"),
    (["service services", "fail failed failing broken crash crashed"],
     "```bash\nsystemctl --failed\n```\nThen `journalctl -u name -b` for why."),
    (["log logs error errors"], "This boot's errors:\n```bash\njournalctl -b -p err\n```"),
    (["ssh", "key keys"],
     "Make a key, send it to the server:\n```bash\nssh-keygen -t ed25519\nssh-copy-id user@host\n```"),
    (["vm vms virtual win11 windows", "start run boot list stop shutdown"],
     "Your VMs:\n```bash\nvirsh -c qemu:///system list --all\nvirsh -c qemu:///system start win11\n```\n"
     "`shutdown win11` to stop it cleanly."),
    (["permission denied"],
     "Check owner and perms:\n```bash\nls -l file\n```\nScript? `chmod +x file`. Owned by root? `sudo chown $USER file`."),
    (["find locate where", "file named called name"],
     "Find by name (case-insensitive):\n```bash\nfind ~ -iname '*name*' 2>/dev/null\n```"),
    (["printer printing"],
     "Settings → Printers: check it's listed and not paused. Clear stuck jobs and restart printing:\n"
     "```bash\ncancel -a\nsudo systemctl restart cups\n```"),
    (["wallpaper"],
     "Right-click the desktop → Change Background…, or:\n```bash\n"
     "gsettings set org.gnome.desktop.background picture-uri 'file:///path/pic.jpg'\n"
     "gsettings set org.gnome.desktop.background picture-uri-dark 'file:///path/pic.jpg'\n```"),
    (["battery"], "```bash\nupower -i $(upower -e | grep BAT)\n```"),
    (["gpu graphics video", "card driver which what"], "```bash\nlspci -k | grep -A3 -i vga\n```"),
    (["usb", "list device devices plugged"], "```bash\nlsusb\nlsblk -f\n```"),
]


def fill_quick(answer, q):
    port = re.search(r"\b(\d{2,5})\b", q)
    size = re.search(r"(\d+)\s*(g|gb|m|mb|k|kb)\b", q, re.I)
    sz = f"{size.group(1)}{size.group(2)[0].upper().replace('K', 'k')}" if size else "500M"
    return answer.replace("{port}", port.group(1) if port else "PORT").replace(
        "{size}", sz).replace("{{}}", "{}")


TIP_WORDS = re.compile(r"\b(tips?|tricks?|hacks?|super ?user|power ?user|pro moves?|"
                       r"something cool|teach me|surprise me)\b", re.I)

# design space: everything is laid out in a 320 x 430 box and scaled
DW, DH = 320.0, 430.0
ART = (58.5, 186.0, 240.0)         # x, y, height of the Tux picture
BUBBLE = (8, 4, 304, 166)          # x, y, w, h
TAIL_TIP = (128, 202)
BELLY = (160, 334, 49, 64)         # cx, cy, rx, ry -> click to type
BELLY_ENTRY = (82, 318, 156, 30)   # where the text entry pops up
BTN_MIN = (220, 198, 9)            # cx, cy, r
BTN_CLOSE = (244, 209, 9)
GRIP = (252, 419, 10)

URL = f"http://127.0.0.1:{PORT}"


# ---------------------------------------------------------------- model server
class Brain:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def healthy(self):
        try:
            with urllib.request.urlopen(URL + "/health", timeout=0.5) as r:
                return r.status == 200
        except Exception:
            return False

    def ensure(self, on_status):
        """Start llama-server if needed and wait until it's ready (worker thread)."""
        with self.lock:
            if self.healthy():
                return True
            if self.proc is None or self.proc.poll() is not None:
                on_status("waking up…")
                cmd = [SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
                       "-t", str(THREADS), "-tb", str(THREADS), "-c", "2048", "-np", "1",
                       "--no-webui"] + SERVER_EXTRA
                if shutil.which("ionice"):
                    cmd = ["ionice", "-c3"] + cmd
                env = dict(os.environ, LD_LIBRARY_PATH=os.path.dirname(SERVER_BIN))
                self.proc = subprocess.Popen(
                    cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    preexec_fn=_child_setup)
            for _ in range(600):  # up to 60 s
                if self.healthy():
                    return True
                if self.proc.poll() is not None:
                    return False
                time.sleep(0.1)
            return False

    def stop(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()


def _child_setup():
    os.nice(15)
    try:  # die with Tux even if Tux crashes
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)
    except Exception:
        pass


# ---------------------------------------------------------------- knowledge: notes, Help, man
STOP = set("the a an to of in on for and or is how do i my what can with from it this that "
           "you me show get use using does make want need why when where which are be "
           "there not way all any some into out up its your have has will just everything "
           "working work problem issue help fix please".split())
SYNONYMS = {
    "wifi": "wireless network", "internet": "network", "folder": "directory folder",
    "delete": "remove delete", "erase": "remove delete", "drive": "disk drive",
    "usb": "usb removable", "wallpaper": "background wallpaper", "screen": "display screen",
    "monitor": "display monitor", "speaker": "sound", "audio": "sound audio",
    "mic": "microphone", "printer": "printer printing", "bluetooth": "bluetooth",
    "ram": "memory", "cpu": "processor cpu", "kill": "kill process", "port": "port network",
    "install": "install package software", "pc": "computer", "bright": "brightness",
    "brightness": "brightness", "frozen": "freeze respond", "hung": "freeze respond",
    "deb": "package install", "lag": "slow performance",
    "bunch": "multiple", "many": "multiple", "several": "multiple", "batch": "multiple",
    "zip": "archive compress zip", "unzip": "extract archive unzip", "battery": "battery power",
    "password": "password", "user": "user account", "sudo": "administrator root sudo",
}


def stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def words(s, expand=False):
    out = set()
    for w in re.findall(r"[a-z0-9][a-z0-9+.-]*", s.lower()):
        w = w.strip(".-")
        if len(w) < 2 or w in STOP:
            continue
        out.add(stem(w))
        if "." in w:  # tar.gz -> tar, gz
            out |= {stem(x) for x in w.split(".") if len(x) > 1}
        if expand and w in SYNONYMS:
            out |= {stem(x) for x in SYNONYMS[w].split()}
    return out


class Knowledge:
    def __init__(self):
        self.notes = []
        try:
            for line in open(NOTES, encoding="utf-8", errors="ignore"):
                if " : " in line:
                    cmd, desc = line.strip().split(" : ", 1)
                    self.notes.append((cmd.strip(), desc.strip(), words(cmd + " " + desc)))
        except OSError:
            pass
        self.help, self.man = [], []
        try:
            idx = json.load(open(INDEX))
            for p in idx["help"]:
                p["tw"], p["dw"], p["bw"] = words(p["title"]), words(p["desc"]), words(p["words"])
                del p["words"]
                self.help.append(p)
            self.man = [(n, d, words(d)) for n, d in idx["man"]]
            df = {}
            for p in self.help:
                for w in p["tw"] | p["dw"]:
                    df[w] = df.get(w, 0) + 1
            n = len(self.help) or 1
            self.idf = {w: math.log(n / c) for w, c in df.items()}
        except (OSError, ValueError, KeyError):
            pass

    def search(self, q):
        qw = words(q, expand=True)
        raw = {w.lower() for w in re.findall(r"[A-Za-z0-9][\w.+-]*", q)}
        mans = []
        for name, desc, dw in self.man:
            s = (6 if name.lower() in raw else 0) + 1.5 * len(qw & dw)
            if s >= 3:
                mans.append((s, name, desc))
        mans.sort(key=lambda t: -t[0])
        best_help, hs = None, 0.0
        idf = getattr(self, "idf", {})
        for p in self.help:  # rare words count more (IDF), so "change"/"file" don't win
            s = (sum(3 * idf.get(w, 0) for w in qw & p["tw"])
                 + sum(2 * idf.get(w, 0) for w in qw & p["dw"])
                 + min(2, 0.25 * len(qw & p["bw"])))
            if s > hs:
                best_help, hs = p, s
        # names a real command (find, tar…)? then it's a terminal question: Help must be very sure
        names_cmd = any(name.lower() in raw for _, name, _ in mans)
        if hs < (HELP_MIN_SCORE * 2 if names_cmd else HELP_MIN_SCORE):
            best_help = None
        note, ns = None, 0
        for cmd, desc, nw in self.notes:
            s = len(qw & nw)
            if s > ns:
                note, ns = (cmd, desc), s
        if ns < 2:
            note = None
        return best_help, [(n, d) for _, n, d in mans[:2]], note

    def quick(self, q):
        qw = words(q)
        for trig, answer in QUICK:
            if all(qw & {stem(w) for w in t.split()} for t in trig):
                return fill_quick(answer, q)
        return None

    def tip(self, n=1):
        pool = POWER_TIPS + [(c, d) for c, d, _ in random.sample(self.notes, min(8, len(self.notes)))]
        picks = random.sample(pool, n)
        return "\n".join(f"Pro tip: `{c}` → {d}" for c, d in picks)


def build_messages(q, history, hp, mans, note):
    """System prompt + style examples + recent chat + question with its References."""
    refs = []
    if hp:
        steps = " / ".join(hp["steps"][:2])
        refs.append(f"GNOME Help '{hp['title']}': {hp['desc']} {steps}"[:260])
    refs += [f"man {n}: {d}" for n, d in mans]
    if note:
        refs.append(f"user's notes: {note[0]} : {note[1]}")
    user = q + ("\n\nReference:\n- " + "\n- ".join(refs) if refs else "")
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for uq, ua in FEWSHOT + history[-HISTORY_TURNS:]:
        msgs += [{"role": "user", "content": uq}, {"role": "assistant", "content": ua}]
    msgs.append({"role": "user", "content": user})
    return msgs


# ---------------------------------------------------------------- text → bubble
def parse_answer(text):
    """Split text into blocks: ('text'|'cmd'|'help', str)."""
    blocks, in_code = [], False
    for raw in text.split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            if line.strip():
                blocks.append(("cmd", line.strip()))
            continue
        m = re.fullmatch(r"\s*(?:\$\s*)?`([^`]+)`\s*[.:]?\s*", line)
        if m:
            blocks.append(("cmd", m.group(1).strip()))
        elif line.startswith("📖 "):
            blocks.append(("help", line))
        elif line.strip():
            blocks.append(("text", line.strip()))
    return blocks


def first_command(text):
    for kind, s in parse_answer(text):
        if kind == "cmd":
            return s
    m = re.search(r"`([^`]+)`", text)
    return m.group(1) if m else None


def esc(s):
    return GLib.markup_escape_text(s)


def inline_markup(s):
    parts = re.split(r"`([^`]*)`", s)
    out = "".join(f"<tt><b>{esc(p)}</b></tt>" if i % 2 else esc(p).replace("**", "")
                  for i, p in enumerate(parts))
    return re.sub(r"^(Pro tip:)", r"<b>\1</b>", out)


# ---------------------------------------------------------------- the window
class Tux(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Tux")
        self.set_decorated(False)
        self.set_default_size(272, 366)
        self.brain = Brain()
        self.kb = Knowledge()
        self.art = cairo.ImageSurface.create_from_png(ART_PNG)
        self.art_runs = json.load(open(ART_JSON))["runs"]
        self.history = []
        self.bubble_text = self.kb.tip() + "\n\nAsk me anything. Click my belly or just type."
        self.help_uri = None
        self.gen = 0
        self.idle_src = None
        self.hover_btns = False
        self.boxes = []              # [(x, y, w, h, kind, payload)] clickable bits of the bubble
        self.region_key = None

        css = Gtk.CssProvider()
        css.load_from_string("""
            window, window.background { background: none; box-shadow: none; }
            entry.tux { border-radius: 14px; background: #ffffff; color: #111;
                        border: 2px solid #f2a900; min-height: 0; padding: 2px 8px; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        overlay = Gtk.Overlay()
        self.set_child(overlay)
        self.area = Gtk.DrawingArea()
        self.area.set_draw_func(self.draw)
        overlay.set_child(self.area)

        self.entry = Gtk.Entry(placeholder_text="ask Tux…")
        self.entry.add_css_class("tux")
        self.entry.set_halign(Gtk.Align.START)
        self.entry.set_valign(Gtk.Align.START)
        self.entry.set_visible(False)
        self.entry.connect("activate", self.on_submit)
        overlay.add_overlay(self.entry)

        click = Gtk.GestureClick(button=1)
        click.connect("pressed", self.on_press)
        self.area.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.on_motion)
        motion.connect("leave", lambda *_: self.set_hover(False))
        self.area.add_controller(motion)

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self.on_scroll)
        self.add_controller(scroll)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

        self.connect("close-request", self.on_close)

    # ---- geometry
    def geom(self):
        w, h = self.area.get_width(), self.area.get_height()
        s = min(w / DW, h / DH) if w and h else 1.0
        return s, (w - DW * s) / 2, (h - DH * s) / 2

    def to_design(self, x, y):
        s, ox, oy = self.geom()
        return (x - ox) / s, (y - oy) / s

    def update_input_region(self):
        """Only Tux's outline, the bubble, buttons and entry take clicks."""
        surf = self.get_surface()
        if not surf:
            return
        s, ox, oy = self.geom()
        key = (round(s, 3), round(ox), round(oy), bool(self.bubble_text), self.entry.get_visible())
        if key == self.region_key:
            return
        self.region_key = key
        R = cairo.RectangleInt
        region = cairo.Region()
        ax, ay, ah = ART
        k = ah / self.art.get_height() * s
        for y, x0, x1 in self.art_runs:
            region.union(R(int(ox + ax * s + x0 * k), int(oy + ay * s + y * k),
                           int((x1 - x0) * k) + 1, int(2 * k) + 1))
        for cx, cy, r in (BTN_MIN, BTN_CLOSE, GRIP):
            r += 3
            region.union(R(int(ox + (cx - r) * s), int(oy + (cy - r) * s),
                           int(2 * r * s) + 1, int(2 * r * s) + 1))
        rects = [BELLY_ENTRY] if self.entry.get_visible() else []
        if self.bubble_text:
            bx, by, bw, bh = BUBBLE
            rects += [BUBBLE, (TAIL_TIP[0] - 22, by + bh, 36, TAIL_TIP[1] - by - bh)]
        for x, y, w, h in rects:
            region.union(R(int(ox + x * s), int(oy + y * s), int(w * s) + 1, int(h * s) + 1))
        surf.set_input_region(region)

    # ---- drawing
    def draw(self, area, cr, w, h):
        s, ox, oy = self.geom()
        cr.save()
        cr.translate(ox, oy)
        cr.scale(s, s)
        ax, ay, ah = ART
        k = ah / self.art.get_height()
        cr.save()
        cr.translate(ax, ay)
        cr.scale(k, k)
        cr.set_source_surface(self.art, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        cr.restore()
        self.draw_buttons(cr)
        cr.restore()
        self.boxes = []
        if self.bubble_text:
            self.draw_bubble(cr, s, ox, oy)
        self.place_entry(s, ox, oy)
        self.update_input_region()

    @staticmethod
    def circle(cr, cx, cy, r):
        cr.new_sub_path()
        cr.arc(cx, cy, r, 0, 2 * math.pi)

    def draw_buttons(self, cr):
        a = 1.0 if self.hover_btns else 0.6
        for (cx, cy, r), col, glyph in ((BTN_MIN, (0.98, 0.75, 0.15), "min"),
                                        (BTN_CLOSE, (0.92, 0.30, 0.25), "close")):
            self.circle(cr, cx, cy, r)
            cr.set_source_rgba(*col, a); cr.fill_preserve()
            cr.set_source_rgba(0, 0, 0, 0.5 * a); cr.set_line_width(1); cr.stroke()
            cr.set_source_rgba(0.1, 0.1, 0.1, a); cr.set_line_width(2)
            k = r * 0.45
            if glyph == "min":
                cr.move_to(cx - k, cy); cr.line_to(cx + k, cy)
            else:
                cr.move_to(cx - k, cy - k); cr.line_to(cx + k, cy + k)
                cr.move_to(cx + k, cy - k); cr.line_to(cx - k, cy + k)
            cr.stroke()
        gx, gy, _ = GRIP  # resize grip: three little lines on the right foot
        cr.set_source_rgba(0.35, 0.2, 0, 0.7); cr.set_line_width(1.6)
        for d in (4, 8, 12):
            cr.move_to(gx + 6 - d, gy + 6); cr.line_to(gx + 6, gy + 6 - d)
        cr.stroke()

    def draw_bubble(self, cr, s, ox, oy):
        bx, by, bw, bh = BUBBLE
        X, Y, W, H, R = ox + bx * s, oy + by * s, bw * s, bh * s, 16 * s
        tx, ty = TAIL_TIP
        cr.new_path()
        cr.arc(X + W - R, Y + R, R, -math.pi / 2, 0)
        cr.arc(X + W - R, Y + H - R, R, 0, math.pi / 2)
        cr.line_to(ox + (tx + 10) * s, Y + H)
        cr.line_to(ox + tx * s, oy + ty * s)
        cr.line_to(ox + (tx - 18) * s, Y + H)
        cr.arc(X + R, Y + H - R, R, math.pi / 2, math.pi)
        cr.arc(X + R, Y + R, R, math.pi, 1.5 * math.pi)
        cr.close_path()
        cr.set_source_rgba(1, 1, 1, 0.97); cr.fill_preserve()
        cr.set_source_rgba(0.1, 0.1, 0.12, 0.85); cr.set_line_width(max(1.5, 2 * s)); cr.stroke()

        pad = 12 * s
        tw, th = W - 2 * pad, H - 2 * pad
        blocks = parse_answer(self.bubble_text) or [("text", self.bubble_text)]
        size = 12.5 * s
        while True:  # shrink text until it fits
            layouts, total = [], 0
            for kind, txt in blocks:
                lay = PangoCairo.create_layout(cr)
                fam = "Monospace" if kind == "cmd" else "Sans"
                lay.set_font_description(
                    Pango.FontDescription.from_string(f"{fam} {max(size, 5):.1f}"))
                lay.set_width(int((tw - (10 * s if kind == "cmd" else 0)) * Pango.SCALE))
                lay.set_wrap(Pango.WrapMode.WORD_CHAR)
                if kind == "cmd":
                    lay.set_text(txt, -1)
                elif kind == "help":
                    lay.set_markup(f"<span foreground='#1a5fb4'><u>{esc(txt)}</u></span>", -1)
                else:
                    lay.set_markup(inline_markup(txt), -1)
                lh = lay.get_pixel_size()[1] + (8 * s if kind == "cmd" else 0)
                layouts.append((kind, txt, lay, lh))
                total += lh + 4 * s
            if total <= th or size <= 6.5 * s:
                break
            size *= 0.9
        y = Y + pad
        cr.save()
        cr.rectangle(X + pad / 2, Y + pad / 2, W - pad, H - pad); cr.clip()
        for kind, txt, lay, lh in layouts:
            if kind == "cmd":
                cr.set_source_rgb(0.13, 0.14, 0.17)
                self.round_rect(cr, X + pad, y, tw, lh, 6 * s); cr.fill()
                cr.set_source_rgb(0.55, 0.95, 0.55)
                cr.move_to(X + pad + 5 * s, y + 4 * s)
                self.boxes.append((X + pad, y, tw, lh, "cmd", txt))
            else:
                if kind == "help":
                    self.boxes.append((X + pad, y, tw, lh, "help", self.help_uri))
                cr.set_source_rgb(0.08, 0.08, 0.1)
                cr.move_to(X + pad, y)
            PangoCairo.show_layout(cr, lay)
            y += lh + 4 * s
        cr.restore()

    @staticmethod
    def round_rect(cr, x, y, w, h, r):
        cr.new_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()

    def place_entry(self, s, ox, oy):
        x, y, w, h = BELLY_ENTRY
        self.entry.set_margin_start(int(ox + x * s))
        self.entry.set_margin_top(int(oy + y * s))
        self.entry.set_size_request(int(w * s), int(h * s))

    # ---- input
    @staticmethod
    def inside_circle(dx, dy, c, extra=2):
        return math.hypot(dx - c[0], dy - c[1]) <= c[2] + extra

    def on_motion(self, _ctl, x, y):
        dx, dy = self.to_design(x, y)
        self.set_hover(100 < dx < 260 and 180 < dy < 300)

    def set_hover(self, v):
        if v != self.hover_btns:
            self.hover_btns = v
            self.area.queue_draw()

    def on_press(self, gesture, n_press, x, y):
        dx, dy = self.to_design(x, y)
        dev, btn, t = gesture.get_device(), gesture.get_current_button(), gesture.get_current_event_time()
        surf = self.get_surface()
        if self.inside_circle(dx, dy, BTN_CLOSE):
            self.close(); return
        if self.inside_circle(dx, dy, BTN_MIN):
            self.minimize(); return
        if self.inside_circle(dx, dy, GRIP, 4):
            surf.begin_resize(Gdk.SurfaceEdge.SOUTH_EAST, dev, btn, x, y, t); return
        for bx, by, bw, bh, kind, payload in self.boxes:
            if bx <= x <= bx + bw and by <= y <= by + bh:
                if kind == "cmd":
                    self.copy(payload)
                elif payload:
                    subprocess.Popen(["yelp", payload], start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        b = BUBBLE
        if self.bubble_text and b[0] <= dx <= b[0] + b[2] and b[1] <= dy <= b[1] + b[3]:
            if not self.history:  # still on the tip screen: show another tip
                self.say(self.kb.tip() + "\n\nAsk me anything. Click my belly or just type.")
            return
        cx, cy, rx, ry = BELLY
        if math.hypot((dx - cx) / rx, (dy - cy) / ry) <= 1 and n_press == 1:
            self.show_entry(); return
        surf.begin_move(dev, btn, x, y, t)

    def on_scroll(self, ctl, _dx, dy):
        if not (ctl.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK):
            return False
        h = self.get_height()
        nh = max(120, min(700, int(h * (0.9 if dy > 0 else 1.1))))
        self.set_default_size(int(nh * DW / DH), nh)
        return True

    def on_key(self, _ctl, keyval, _code, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and keyval in (Gdk.KEY_q, Gdk.KEY_Q, Gdk.KEY_w):
            self.close(); return True
        if keyval == Gdk.KEY_Escape:
            if self.entry.get_visible():
                self.entry.set_visible(False); self.region_key = None; self.area.queue_draw()
            return True
        ch = Gdk.keyval_to_unicode(keyval)
        if not ctrl and ch >= 32 and not self.entry.get_visible():
            self.show_entry(chr(ch)); return True
        return False

    def show_entry(self, prefix=""):
        if not self.entry.get_visible():
            threading.Thread(target=self.prewarm, args=(self.gen,), daemon=True).start()
        self.entry.set_visible(True)
        self.region_key = None
        self.area.queue_draw()
        self.entry.grab_focus()
        if prefix:
            self.entry.set_text(prefix)
            self.entry.set_position(-1)

    def copy(self, text):
        self.get_clipboard().set(text)
        if "(copied" not in self.bubble_text:
            self.say(self.bubble_text.rstrip() + "\n(copied ✓)")

    def say(self, text):
        if bool(text) != bool(self.bubble_text):
            self.region_key = None
        self.bubble_text = text
        self.area.queue_draw()
        return False

    # ---- asking
    def on_submit(self, entry):
        q = entry.get_text().strip()
        entry.set_text("")
        if not q:
            return
        self.gen += 1
        if self.idle_src:
            GLib.source_remove(self.idle_src); self.idle_src = None
        if TIP_WORDS.search(q):  # tips come from the curated list, instantly, no model
            self.say("Power moves:\n" + self.kb.tip(3))
            return
        hp, mans, note = self.kb.search(q)
        self.help_uri = hp["uri"] if hp else None
        quick = self.kb.quick(q)
        if quick:  # hand-checked answer: instant, no model
            self.say(quick + (f"\n📖 {hp['title']} ›" if hp else ""))
            return
        # instant answer from your notes while the model thinks
        self.say(f"From your notes:\n`{note[0]}`\n\nthinking…" if note else "thinking…")
        threading.Thread(target=self.ask, args=(q, self.gen, hp, mans, note), daemon=True).start()

    def ask(self, q, gen, hp, mans, note):
        def status(msg):
            if gen == self.gen:
                GLib.idle_add(self.say, msg)

        if not os.path.exists(MODEL) or not os.path.exists(SERVER_BIN):
            status("My brain isn't installed yet (model or llama-server missing).")
            return
        if not self.brain.ensure(status):
            status("Couldn't start my brain (llama-server). Try again?")
            return
        msgs = build_messages(q, self.history, hp, mans, note)
        body = json.dumps({"messages": msgs, "stream": True, "max_tokens": MAX_TOKENS,
                           "temperature": 0.2, "cache_prompt": True}).encode()
        req = urllib.request.Request(URL + "/v1/chat/completions", body,
                                     {"Content-Type": "application/json"})
        out = ""
        footer = f"\n📖 {hp['title']} ›" if hp else ""
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                last = 0.0
                for raw in r:
                    if gen != self.gen:
                        return
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"].get("content") or ""
                    except (ValueError, KeyError, IndexError):
                        continue
                    out += delta
                    now = time.monotonic()
                    if now - last > 0.05:  # cap redraws at ~20/s
                        last = now
                        status(out)
        except Exception as e:
            status(f"Oops, my brain hiccuped: {e}")
            return
        out = out.strip()
        status(out + footer if "only know Linux" not in out else out)
        self.history.append((q, out))
        self.history = self.history[-HISTORY_TURNS:]
        GLib.idle_add(self.arm_idle_timer, gen)

    def prewarm(self, gen):
        """Load the model and pre-read the fixed prompt while you type."""
        if not (os.path.exists(MODEL) and os.path.exists(SERVER_BIN)):
            return
        if not self.brain.ensure(lambda m: None):
            return
        body = json.dumps({"messages": build_messages("hi", [], None, [], None),
                           "max_tokens": 1, "cache_prompt": True}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(
                URL + "/v1/chat/completions", body, {"Content-Type": "application/json"}),
                timeout=60).read()
        except Exception:
            pass
        GLib.idle_add(self.arm_idle_timer, gen)

    def arm_idle_timer(self, gen):
        if gen != self.gen:
            return False
        if self.idle_src:
            GLib.source_remove(self.idle_src)
        self.idle_src = GLib.timeout_add_seconds(IDLE_SECS, self.on_idle)
        return False

    def on_idle(self):
        self.idle_src = None
        threading.Thread(target=self.brain.stop, daemon=True).start()
        return False

    def on_close(self, *_):
        self.gen += 1
        self.brain.stop()
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="local.tux.helper")

    def do_activate(self):
        if not (os.path.exists(ART_PNG) and os.path.exists(INDEX)):
            subprocess.run(["python3", os.path.join(HERE, "setup_tux.py")])
        win = self.get_active_window() or Tux(self)
        win.present()
        # test hooks: TUX_ASK="question" auto-asks, TUX_SNAPSHOT=file.png saves a render
        if os.environ.get("TUX_ASK"):
            GLib.timeout_add(500, lambda: (win.entry.set_text(os.environ["TUX_ASK"]),
                                           win.on_submit(win.entry), False)[-1])
        if os.environ.get("TUX_SNAPSHOT"):
            GLib.timeout_add(int(os.environ.get("TUX_SNAPSHOT_MS", 1500)),
                             lambda: (snapshot(win, os.environ["TUX_SNAPSHOT"]), False)[-1])


def snapshot(win, path):
    paintable = Gtk.WidgetPaintable.new(win)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, win.get_width(), win.get_height())
    node = snap.to_node()
    if node:
        win.get_native().get_renderer().render_texture(node, None).save_to_png(path)
    print("snapshot", path, flush=True)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    App().run(None)
