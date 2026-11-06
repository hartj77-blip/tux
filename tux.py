#!/usr/bin/env python3
"""Tux - a tiny local Linux helper. A penguin-shaped window with a speech bubble.

Answers come from a small local model, grounded with the best matches from
GNOME Help, man pages and your own notes. The model server only runs while
you're asking things and stops after IDLE_SECS idle (and when Tux closes).
Run setup_tux.py once to build tux-art.png and index.json.
"""
import ctypes
import ctypes.util
import glob
import json
import math
import os
import random
import re
import shlex
import shutil
import sys
import signal
import subprocess
import threading
import time
import urllib.request
import warnings

import cairo
import gi

from knowledge import Library, best_window, have_arch  # Tux's offline Linux library
from explain import NO_HELP, as_text, breakdown, looks_like_command, strip_ask  # "what would this do?"

# software rendering: ~half the RAM of GTK's GL renderer and leaves the GPU alone
os.environ.setdefault("GSK_RENDERER", "cairo")
# GNOME on Wayland doesn't let apps stay on top or place themselves; X11 apps may. So Tux
# runs through XWayland (part of Ubuntu, nothing to install). TUX_WAYLAND=1 opts out.
USE_X11 = bool(os.environ.get("DISPLAY")) and not os.environ.get("TUX_WAYLAND")
_ORIG_GDK_BACKEND = os.environ.get("GDK_BACKEND")
if USE_X11:
    os.environ["GDK_BACKEND"] = "x11"

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

# ---------------------------------------------------------------- settings
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_BIN = next(iter(sorted(glob.glob(os.path.join(HERE, "bin", "*", "llama-server")))),
                  os.path.join(HERE, "bin", "llama-server"))
MODEL = os.path.join(HERE, "models", "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf")
ART_PNG = os.path.join(HERE, "tux-art.png")
ART_JSON = os.path.join(HERE, "tux-art.json")
INDEX = os.path.join(HERE, "index.json")
NOTES = os.path.expanduser("~/Helpful Linux Commands.txt")
HISTFILE = os.environ.get("HISTFILE", os.path.expanduser("~/.bash_history"))
VERSION = "2"
CLIP_MAX = 900          # chars of clipboard text the model sees
RECENT_CMDS = 8         # recent terminal commands used for "that"/"it" questions
PORT = 8088
THREADS = 3            # leave the rest of the CPU for VMs / remote sessions
IDLE_SECS = int(os.environ.get("TUX_IDLE_SECS", 300))  # unload model after this
HIDE_SECS = int(os.environ.get("TUX_HIDE_SECS", 20))   # tuck the bubble away after this idle
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
    "destructive commands. If Clipboard or Recent commands are given, that's what the "
    "user means by this/that/it (the last command is the likeliest), and never refuse "
    "then. Only for clearly non-computer topics (sports, jokes, poems) "
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
    ("what's this?\n\nClipboard:\n```\nE: Could not get lock /var/lib/dpkg/lock-frontend\n```",
     "Another apt/updater is running. Wait for it, or see who:\n"
     "```bash\nps aux | grep -E 'apt|dpkg' | grep -v grep\n```\nDon't delete the lock file."),
    ("why did that fail?\n\nRecent terminal commands (last = most recent):\ncd ~/app\nnpm start",
     "Looks like `npm start`. Usually missing packages:\n```bash\nnpm install\n```\n"
     "Copy the error and click me for an exact fix."),
    ("tell me a joke", "I only know Linux stuff!"),
]
# "what would this command do?": one plain sentence on top of the docs breakdown
EXPLAIN_PROMPT = (
    "You are Tux, a Linux helper. The user shows a shell command they have NOT run. Say what "
    "it would do in ONE short plain sentence (at most 18 words). No code block, don't repeat "
    "the command, no advice. Use the Parts list."
)
EXPLAIN_FEWSHOT = [
    ("du -sh * | sort -h\n\nParts:\n- du: estimate file space usage\n- -s: display only a total "
     "for each argument\n- -h: print sizes in human readable format\n- | sends that output into\n"
     "- sort: sort lines of text files\n- -h: compare human readable numbers",
     "Lists the size of everything in this folder, smallest to biggest."),
    ("rm -rf build\n\nParts:\n- rm: remove files or directories\n- -r: remove directories and "
     "their contents recursively\n- -f: ignore nonexistent files, never prompt",
     "Deletes the build folder and everything in it, without asking."),
]
EXPLAIN_ASK = re.compile(r"\b(explain|what|whats|safe|mean|means|do|does|break ?down)\b", re.I)
# questions that point at something: "why did that fail", "undo it", "explain this"
REFERS = re.compile(r"\b(this|that|it|these|those|last|previous|just|above|error|failed|"
                    r"fail|wrong|why|explain|mean|means|undo|again|fix)\b", re.I)
SECRETS = re.compile(r"pass(word|wd)?|token|secret|api[_-]?key|private|authorization|"
                     r"bearer|--pass|credential", re.I)
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
    (["tar tgz untar", "extract untar open unpack decompress unzip"], "Extract any tarball (gz/xz/bz2 auto-detected):\n```bash\ntar -xf file.tar.gz\n```"),
    (["unzip"], "```bash\nunzip file.zip -d folder/\n```"),
    (["zip", "folder directory create make compress"],
     "Zip a folder (and everything in it):\n```bash\nzip -r folder.zip folder/\n```"),
    (["service services", "fail failed failing broken crash crashed"],
     "```bash\nsystemctl --failed\n```\nThen `journalctl -u name -b` for why."),
    (["log logs error errors", "error errors boot system crash wrong"], "This boot's errors:\n```bash\njournalctl -b -p err\n```"),
    (["ssh", "key keys"],
     "Make a key, send it to the server:\n```bash\nssh-keygen -t ed25519\nssh-copy-id user@host\n```"),
    (["vm vms virtual", "start run boot list stop shutdown"],
     "Your VMs, then start one by name:\n```bash\nvirsh -c qemu:///system list --all\n"
     "virsh -c qemu:///system start NAME\n```\n`shutdown NAME` stops it cleanly."),
    (["permission denied"],
     "Check owner and perms:\n```bash\nls -l file\n```\nScript? `chmod +x file`. Owned by root? `sudo chown $USER file`."),
    (["find locate where", "named called name"],
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
    (["driver drivers", "install get update missing nvidia graphics gpu wifi"],
     "Let Ubuntu pick the right drivers (GPU, Wi-Fi):\n```bash\nsudo ubuntu-drivers install\n```\n"
     "Or: Software & Updates → Additional Drivers."),
    (["rename", "multiple bunch many all several batch lot"],
     "In Files: select them all, press F2 for bulk rename. Terminal, e.g. .txt → .md:\n"
     "```bash\nfor f in *.txt; do mv -- \"$f\" \"${f%.txt}.md\"; done\n```"),
    (["appimage"],
     "Make it runnable, then run it:\n```bash\nchmod +x App.AppImage\n./App.AppImage\n```\n"
     "Won't start? It may need FUSE: `sudo apt install libfuse2t64`"),
]


def fill_quick(answer, q):
    port = re.search(r"\b(\d{2,5})\b", q)
    size = re.search(r"(\d+)\s*(g|gb|m|mb|k|kb)\b", q, re.I)
    sz = f"{size.group(1)}{size.group(2)[0].upper().replace('K', 'k')}" if size else "500M"
    return answer.replace("{port}", port.group(1) if port else "PORT").replace(
        "{size}", sz).replace("{{}}", "{}")


TIP_WORDS = re.compile(r"\b(tips?|tricks?|hacks?|super ?user|power ?user|pro moves?|"
                       r"something cool|teach me|surprise me)\b", re.I)

# ---------------------------------------------------------------- window modes: settings + X11
SETTINGS_FILE = os.environ.get("TUX_SETTINGS") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "tux", "settings.json")
ON_TOP_WORDS = re.compile(r"\b(on top|always visible|stay visible|pin (yourself|you)|mini tux|"
                          r"(sit|stand|stay|live) on (the|my) (dock|taskbar|toolbar|panel|screen edge))\b",
                          re.I)


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(d):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE + ".tmp", "w") as f:
            json.dump(d, f)
        os.replace(SETTINGS_FILE + ".tmp", SETTINGS_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------- keyboard shortcut
# A GNOME custom shortcut (Settings → Keyboard → Custom Shortcuts) runs `tux.py --toggle`:
# Tux pops up ready to type, and the same keys put him away again.
SHORTCUT = "<Control><Alt>space"
TOGGLE_CMD = f"python3 {shlex.quote(os.path.join(HERE, 'tux.py'))} --toggle"
KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
KEY_SCHEMA = KEYS_SCHEMA + ".custom-keybinding"
KEY_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/tux/"
TAKEN_IN = ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings",
            "org.gnome.mutter.keybindings", "org.gnome.mutter.wayland.keybindings", KEYS_SCHEMA)
SHORTCUT_HOW = ("Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts, "
                "add one that runs:\n```bash\n" + TOGGLE_CMD + "\n```")


def _has_schema(sid):
    src = Gio.SettingsSchemaSource.get_default()
    return bool(src and src.lookup(sid, True))


def _accel(a):
    ok, key, mods = Gtk.accelerator_parse(a or "")
    return (Gdk.keyval_to_lower(key), int(mods)) if ok and key else None


def shortcut_get():
    """Tux's keyboard shortcut as GNOME has it: the keys ("" = none set up), or None
    if this desktop has no GNOME custom shortcuts."""
    if not (_has_schema(KEYS_SCHEMA) and _has_schema(KEY_SCHEMA)):
        return None
    if KEY_PATH not in Gio.Settings.new(KEYS_SCHEMA).get_strv("custom-keybindings"):
        return ""
    return Gio.Settings.new_with_path(KEY_SCHEMA, KEY_PATH).get_string("binding")


def shortcut_label(keys):
    ok, key, mods = Gtk.accelerator_parse(keys or "")
    return Gtk.accelerator_get_label(key, mods) if ok and key else (keys or "")


def shortcut_taken(keys):
    """What in GNOME already uses these keys, or None."""
    want = _accel(keys)
    for sid in TAKEN_IN:
        if not _has_schema(sid):
            continue
        st = Gio.Settings.new(sid)
        for name in st.props.settings_schema.list_keys():
            v = st.get_value(name)
            t = v.get_type_string()
            vals = v.unpack() if t == "as" else [v.unpack()] if t == "s" else []
            if want in map(_accel, vals):
                return f"GNOME's “{name.replace('-', ' ')}” shortcut"
    for path in Gio.Settings.new(KEYS_SCHEMA).get_strv("custom-keybindings"):
        other = Gio.Settings.new_with_path(KEY_SCHEMA, path)
        if path != KEY_PATH and _accel(other.get_string("binding")) == want:
            return f"your custom shortcut “{other.get_string('name') or path}”"
    return None


def shortcut_set(on):
    """Turn Tux's GNOME shortcut on or off -> (ok, message). Turning it on again keeps
    keys you picked yourself and points it at this copy of Tux."""
    keys = shortcut_get()
    if keys is None:
        return False, "This desktop has no GNOME shortcuts. In its keyboard settings, " \
                      f"add a shortcut that runs:  {TOGGLE_CMD}"
    media = Gio.Settings.new(KEYS_SCHEMA)
    paths = media.get_strv("custom-keybindings")
    mine = Gio.Settings.new_with_path(KEY_SCHEMA, KEY_PATH)
    if not on:
        if KEY_PATH in paths:
            media.set_strv("custom-keybindings", [p for p in paths if p != KEY_PATH])
        for k in ("name", "command", "binding"):
            mine.reset(k)
        Gio.Settings.sync()
        return True, "Keyboard shortcut off."
    if not keys:
        taken = shortcut_taken(SHORTCUT)
        if taken:
            return False, f"{shortcut_label(SHORTCUT)} is already {taken}."
        keys = SHORTCUT
    mine.set_string("name", "Tux")
    mine.set_string("command", TOGGLE_CMD)
    mine.set_string("binding", keys)
    if KEY_PATH not in paths:
        media.set_strv("custom-keybindings", paths + [KEY_PATH])
    Gio.Settings.sync()
    return True, f"Keyboard shortcut: {shortcut_label(keys)}"


def shortcut_cli(args):
    """`tux.py --shortcut on|off|status`, for install.sh and uninstall.sh."""
    if args[:1] == ["status"]:
        keys = shortcut_get()
        print("unsupported" if keys is None else f"on {shortcut_label(keys)}" if keys else "off")
        return 0
    if args[:1] in (["on"], ["off"]):
        ok, msg = shortcut_set(args[0] == "on")
        print(msg)
        if not ok and shortcut_get() is not None:  # the keys are taken: say how to pick others
            print("Pick your own: Settings → Keyboard → View and Customize Shortcuts → Custom "
                  f"Shortcuts, add one that runs:  {TOGGLE_CMD}")
        return 0 if ok else 1
    print("usage: tux.py --shortcut on|off|status")
    return 2


def clean_env():
    """Environment for apps Tux opens (Help): don't push them onto X11 too."""
    env = dict(os.environ)
    if _ORIG_GDK_BACKEND is None:
        env.pop("GDK_BACKEND", None)
    else:
        env["GDK_BACKEND"] = _ORIG_GDK_BACKEND
    return env


class _ClientMessage(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong),
                ("message_type", ctypes.c_ulong), ("format", ctypes.c_int),
                ("data", ctypes.c_long * 5), ("pad", ctypes.c_long * 12)]


class X11:
    """Tiny Xlib bridge: stay on top, move, find the pointer and the screen's usable area."""

    @classmethod
    def open(cls):
        try:
            return cls()
        except (OSError, RuntimeError, AttributeError):
            return None

    def __init__(self):
        lib = ctypes.util.find_library("X11")
        if not lib:
            raise RuntimeError("no libX11")
        X = self.X = ctypes.CDLL(lib)
        vp, ul, ci, cl = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long
        P = ctypes.POINTER
        X.XOpenDisplay.restype, X.XOpenDisplay.argtypes = vp, [ctypes.c_char_p]
        X.XDefaultRootWindow.restype, X.XDefaultRootWindow.argtypes = ul, [vp]
        X.XInternAtom.restype, X.XInternAtom.argtypes = ul, [vp, ctypes.c_char_p, ci]
        X.XMoveWindow.argtypes = [vp, ul, ci, ci]
        X.XFlush.argtypes = [vp]
        X.XFree.argtypes = [vp]
        X.XSendEvent.argtypes = [vp, ul, ci, cl, vp]
        X.XGetWindowProperty.argtypes = [vp, ul, ul, cl, cl, ci, ul, P(ul), P(ci), P(ul), P(ul), P(vp)]
        X.XTranslateCoordinates.argtypes = [vp, ul, ul, ci, ci, P(ci), P(ci), P(ul)]
        X.XQueryPointer.argtypes = [vp, ul, P(ul), P(ul), P(ci), P(ci), P(ci), P(ci), P(ctypes.c_uint)]
        X.XDisplayWidth.argtypes = X.XDisplayHeight.argtypes = [vp, ci]
        self.dpy = X.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("no X display")
        self.root = X.XDefaultRootWindow(self.dpy)
        atom = lambda n: X.XInternAtom(self.dpy, n.encode(), 0)  # noqa: E731
        self.STATE, self.ABOVE = atom("_NET_WM_STATE"), atom("_NET_WM_STATE_ABOVE")
        self.STICKY, self.WORKAREA = atom("_NET_WM_STATE_STICKY"), atom("_NET_WORKAREA")
        self.ACTIVE = atom("_NET_ACTIVE_WINDOW")

    def set_on_top(self, xid, on):
        """Above other windows + on every workspace (or neither)."""
        ev = _ClientMessage(type=33, send_event=1, display=self.dpy, window=xid,
                            message_type=self.STATE, format=32)
        ev.data[0], ev.data[1], ev.data[2], ev.data[3] = (1 if on else 0), self.ABOVE, self.STICKY, 1
        self.X.XSendEvent(self.dpy, self.root, 0, (1 << 20) | (1 << 19), ctypes.byref(ev))
        self.X.XFlush(self.dpy)

    def activate(self, xid):
        """Ask the window manager to raise and focus Tux. Sent the way a taskbar does, so
        it works when the keyboard shortcut calls him from the background."""
        ev = _ClientMessage(type=33, send_event=1, display=self.dpy, window=xid,
                            message_type=self.ACTIVE, format=32)
        ev.data[0], ev.data[1], ev.data[2] = 2, 0, 0   # from a pager, current time, no window
        self.X.XSendEvent(self.dpy, self.root, 0, (1 << 20) | (1 << 19), ctypes.byref(ev))
        self.X.XFlush(self.dpy)

    def move(self, xid, x, y):
        self.X.XMoveWindow(self.dpy, xid, int(x), int(y))
        self.X.XFlush(self.dpy)

    def pos(self, xid):
        x, y, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        self.X.XTranslateCoordinates(self.dpy, xid, self.root, 0, 0, ctypes.byref(x),
                                     ctypes.byref(y), ctypes.byref(child))
        return x.value, y.value

    def pointer(self):
        r, c = ctypes.c_ulong(), ctypes.c_ulong()
        rx, ry, wx, wy, m = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_uint()
        self.X.XQueryPointer(self.dpy, self.root, ctypes.byref(r), ctypes.byref(c), ctypes.byref(rx),
                             ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(m))
        return rx.value, ry.value

    def workarea(self):
        """(x, y, w, h) of the screen minus the top bar and dock."""
        t, f, n, after, prop = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
        self.X.XGetWindowProperty(self.dpy, self.root, self.WORKAREA, 0, 4, 0, 6, ctypes.byref(t),
                                  ctypes.byref(f), ctypes.byref(n), ctypes.byref(after), ctypes.byref(prop))
        if prop.value and n.value >= 4:
            arr = ctypes.cast(prop, ctypes.POINTER(ctypes.c_long))
            wa = tuple(int(arr[i]) for i in range(4))
            self.X.XFree(prop)
            if wa[2] > 0 and wa[3] > 0:
                return wa
        return 0, 0, self.X.XDisplayWidth(self.dpy, 0), self.X.XDisplayHeight(self.dpy, 0)


# ---------------------------------------------------------------- install helper
# "install X" / "remove X" / "is X installed": checked against this machine's real package
# lists (apt, dpkg, snap), so the command Tux gives is one that will actually work here.
INSTALL_RX = re.compile(r"\b(?P<verb>reinstall|uninstall|install|remove|purge|get rid of)\b"
                        r"\s+(?P<rest>.+)", re.I)
INSTALLED_RX = re.compile(r"\b(?:is|are)\s+(?P<rest>.+?)\s+installed\b|"
                          r"\bdo i have\s+(?P<rest2>.+?)(?:\s+installed)?\s*\??$", re.I)
NOT_PKG = {"updates", "update", "upgrades", "upgrade", "driver", "drivers", "deb", "appimage",
           "file", "files", "folder", "directory", "it", "this", "that", "everything", "stuff",
           "app", "apps", "program", "programs", "software", "package", "packages", "fonts",
           "font", "theme", "themes", "extension", "extensions", "printer", "ubuntu", "linux"}
# common names that don't match their package, or that are best from somewhere specific
INSTALL_ALIASES = {
    "vs code": ("snap", "code", "--classic"), "vscode": ("snap", "code", "--classic"),
    "visual studio code": ("snap", "code", "--classic"),
    "chrome": ("chrome",), "google chrome": ("chrome",),
    "discord": ("snap", "discord"), "spotify": ("snap", "spotify"), "slack": ("snap", "slack"),
    "zoom": ("snap", "zoom-client"), "telegram": ("snap", "telegram-desktop"),
    "obs": ("apt", "obs-studio"), "obs studio": ("apt", "obs-studio"),
    "docker": ("apt", "docker.io", "Use it without sudo: `sudo usermod -aG docker $USER`, "
               "then log out and back in."),
    "node": ("apt", "nodejs npm"), "nodejs": ("apt", "nodejs npm"), "node.js": ("apt", "nodejs npm"),
    "npm": ("apt", "nodejs npm"), "steam": ("apt", "steam-installer"),
    "java": ("apt", "default-jdk"), "jdk": ("apt", "default-jdk"),
    "pip": ("apt", "python3-pip"), "pip3": ("apt", "python3-pip"), "python": ("apt", "python3"),
    "office": ("apt", "libreoffice"),
}
CHROME_DEB = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"


def _run(cmd, timeout=3):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def install_target(q):
    """('install'|'remove'|'check', name) if q is about installing something, else None."""
    m = INSTALL_RX.search(q)
    verb = None
    if m:
        verb = {"uninstall": "remove", "purge": "remove", "get rid of": "remove",
                "reinstall": "install"}.get(m.group("verb").lower(), m.group("verb").lower())
        rest = m.group("rest")
    else:
        m = INSTALLED_RX.search(q)
        if not m:
            return None
        verb, rest = "check", m.group("rest") or m.group("rest2")
    rest = re.split(r"\s+(?:on|in|for|from|to|with|using|via|please)\b|[?,!]", rest.lower())[0]
    words_ = [w for w in rest.split() if w not in ("the", "a", "an", "my", "some")]
    while words_ and words_[-1] in ("app", "application", "program", "software", "package"):
        words_.pop()
    name = " ".join(words_[:3]).strip(" .")
    if not name or name.startswith(".") or "/" in name or \
            any(w.strip(".") in NOT_PKG for w in name.split()):
        return None
    return verb, name


def _installed(pkg):
    code, out = _run(["dpkg-query", "-W", "-f=${Status}|${Version}", pkg])
    if code == 0 and out.startswith("install ok installed"):
        return "apt", out.split("|", 1)[1]
    if shutil.which("snap"):
        code, out = _run(["snap", "list", pkg])
        if code == 0 and len(out.splitlines()) > 1:
            return "snap", out.splitlines()[1].split()[1]
    return None


def nice_version(v):
    """'1:2.53.0-1ubuntu1.1' -> '2.53.0', '1:1.0.0.85~ds' -> '1.0.0.85'."""
    return re.split(r"[-~+]", v.split(":", 1)[-1])[0]


def _apt_candidate(pkg):
    code, out = _run(["apt-cache", "policy", pkg])
    m = re.search(r"Candidate:\s*(\S+)", out)
    return m.group(1) if m and m.group(1) != "(none)" else None


def _snap_available(pkg):
    """None if not in the Snap Store, else True/False for 'needs --classic'."""
    if not shutil.which("snap"):
        return None
    code, out = _run(["snap", "info", pkg], timeout=4)
    if code != 0:
        return None
    return bool(re.search(r"^\s*\S+/stable:.*\bclassic\b", out, re.M))


def _apt_suggestions(name, n=3):
    stem_ = re.escape(name.replace(" ", "-"))
    code, out = _run(["apt-cache", "search", "--names-only", f"^{stem_}"])
    hits = []
    for line in out.splitlines():
        pkg, _, desc = line.partition(" - ")
        if re.search(r"(^lib|-dev$|-doc$|-dbg$|-data$|-common$)", pkg):
            continue
        hits.append((len(pkg), pkg, desc))
    return [(p, d) for _, p, d in sorted(hits)[:n]]


def install_answer(verb, name):
    """A ready answer for this machine, or None to let the model handle it."""
    alias = INSTALL_ALIASES.get(name)
    if alias and alias[0] == "chrome":
        if _installed("google-chrome-stable"):
            return "Google Chrome is already installed. 👍"
        return ("Chrome isn't in Ubuntu's software; get Google's .deb:\n"
                f"```bash\nwget {CHROME_DEB}\nsudo apt install ./google-chrome-stable_current_amd64.deb\n```\n"
                "It adds Google's repo, so it updates with the rest of your system.")
    pkgs = alias[1].split() if alias else [name.replace(" ", "-")]
    pkg = pkgs[0]
    inst = _installed(pkg)
    if verb == "remove":
        if not inst:
            return f"`{pkg}` isn't installed, so nothing to remove." if alias else None
        src, ver = inst
        if src == "snap":
            return f"Remove the snap:\n```bash\nsudo snap remove {pkg}\n```"
        return (f"Remove it:\n```bash\nsudo apt remove {pkg}\n```\n"
                "Pro tip: `sudo apt autoremove` clears out what it pulled in.")
    if inst:
        src, ver = inst
        return f"Already installed: `{pkg}` {nice_version(ver)} ({src}). 👍"
    if verb == "check":
        found = _apt_candidate(pkg) or (_snap_available(pkg) is not None)
        return (f"`{pkg}` isn't installed" + (", but it's available: ask me to install it." if found
                                                else ", and I can't find it in apt or snap."))
    note = ("\n" + alias[2]) if alias and len(alias) > 2 and alias[0] == "apt" else ""
    if alias and alias[0] == "snap":
        classic = " --classic" if len(alias) > 2 else ""
        return f"From the Snap Store:\n```bash\nsudo snap install {pkg}{classic}\n```"
    ver = _apt_candidate(pkg)
    if ver:
        return (f"From Ubuntu's software (apt), version {nice_version(ver)}:\n"
                f"```bash\nsudo apt install {' '.join(pkgs)}\n```" + note)
    snap = _snap_available(pkg)
    if snap is not None:
        return f"From the Snap Store:\n```bash\nsudo snap install {pkg}{' --classic' if snap else ''}\n```"
    sugg = _apt_suggestions(name)
    if sugg:
        lines = "\n".join(f"• `{p}` → {d}" for p, d in sugg)
        return (f"No package called \"{name}\". Closest matches:\n{lines}\n"
                f"```bash\nsudo apt install {sugg[0][0]}\n```")
    return None

# ---------------------------------------------------------------- layouts
ART_H = 486  # height of the Tux picture in pixels; belly etc. are measured on it


class Layout:
    """Where everything goes, in design units. Free Tux scales them to the window size;
    Mini Tux is drawn 1:1 in a window of exactly DW x DH."""

    def __init__(self, mode, edge, DW, DH, ART, BUBBLE, TAIL_TIP, ENTRY, BTN_MIN, BTN_CLOSE,
                 GRIP=None, HOVER=None, TEXT_TOP=0, FONT=12.5, SIDE="right"):
        self.mode, self.edge, self.DW, self.DH = mode, edge, DW, DH
        self.ART, self.BUBBLE, self.TAIL_TIP, self.ENTRY = ART, BUBBLE, TAIL_TIP, ENTRY
        self.BTN_MIN, self.BTN_CLOSE, self.GRIP = BTN_MIN, BTN_CLOSE, GRIP
        self.HOVER, self.TEXT_TOP, self.FONT, self.SIDE = HOVER, TEXT_TOP, FONT, SIDE

    @property
    def BELLY(self):  # cx, cy, rx, ry: click here to type
        ax, ay, ah = self.ART
        k = ah / ART_H
        return ax + 205.5 * k, ay + 299.7 * k, 99 * k, 130 * k

    def tail(self):
        """Speech-bubble tail: which side it leaves from, two base points and the tip."""
        bx, by, bw, bh = self.BUBBLE
        tx, ty = self.TAIL_TIP
        hw, r = 11, 16
        side = ("bottom" if ty > by + bh else "top" if ty < by else "left" if tx < bx else "right")
        if side in ("top", "bottom"):
            c = min(max(tx, bx + r + hw), bx + bw - r - hw)
            yy = by + bh if side == "bottom" else by
            return side, (c - hw, yy), (c + hw, yy), (tx, ty)
        c = min(max(ty, by + r + hw), by + bh - r - hw)
        xx = bx if side == "left" else bx + bw
        return side, (xx, c - hw), (xx, c + hw), (tx, ty)


ART_W = 411  # width of the Tux picture
BUBBLE_SIDES = ("right", "left", "above", "below")   # where the bubble sits, seen from Tux


def bubble_layout(mode, edge, side, th, bw, bh, grip=False, font=12.5, valign="bottom",
                  halign="left", btn_r=7):
    """Tux and his speech bubble, the bubble on any side of him. The text box and the
    minimize/close buttons live inside the bubble."""
    tw = th * ART_W / ART_H
    gap = max(9, th * 0.1)          # room for the tail
    if side in ("right", "left"):
        DW, DH = tw + gap + bw + 8, max(bh + 8, th + 4)
        ay = {"bottom": DH - th, "top": 0}.get(valign, (DH - th) / 2)
        by = {"bottom": DH - bh - 4, "top": 4}.get(valign, (DH - bh) / 2)
        if side == "right":
            ax, bx = 0, tw + gap
            tip = (ax + 0.80 * tw, ay + 0.22 * th)   # the tail points at his head
        else:
            ax, bx = DW - tw, 4
            tip = (ax + 0.20 * tw, ay + 0.22 * th)
    else:
        DW, DH = max(bw + 8, tw + 8), th + gap + bh + 8
        ax, bx = (4 if halign == "left" else DW - tw - 4), 4
        if side == "above":
            by, ay = 4, DH - th
            tip = (ax + 0.5 * tw, ay + 0.04 * th)
        else:
            ay, by = 0, th + gap + 4
            tip = (ax + 0.5 * tw, ay + 0.96 * th)
    return Layout(mode, edge, float(DW), float(DH), ART=(ax, ay, th), BUBBLE=(bx, by, bw, bh),
                  TAIL_TIP=tip, ENTRY=(bx + 8, by + bh - 34, bw - 16, 26),
                  BTN_MIN=(bx + bw - 5.4 * btn_r, by + 2 * btn_r, btn_r),
                  BTN_CLOSE=(bx + bw - 2.3 * btn_r, by + 2 * btn_r, btn_r),
                  GRIP=(ax + tw - 8, ay + th - 8, 9) if grip else None,
                  TEXT_TOP=2 * btn_r, FONT=font, SIDE=side)


# The box is narrow while Tux is idle or you're typing, and widens for an answer until a
# command this many characters long fits on one line (most of Tux's commands are shorter).
CMD_FIT = 44
CMD_PAD = 36   # room around a command line inside the box: margins + its dark background
FREE_BW = 282       # Free Tux's narrow box, in design units (everything scales with his window)
FREE_BH_MAX = 270   # ...and the tallest it grows for a long answer


def free_layout(side="right", bw=FREE_BW, bh=None):
    """Free Tux: move and resize anywhere (everything scales with the window)."""
    bh = bh or (150 if side in ("above", "below") else 172)
    return bubble_layout("free", None, side, th=150, bw=bw, bh=bh, grip=True)


def free_default_size(L):
    return int(L.DW * 0.88), int(L.DH * 0.88)


# Mini Tux: the lowest-profile version, a tiny penguin and a text box that fits its message
MINI_TH, MINI_BW, MINI_FONT = 44, 232, 10.5
MINI_BH_MIN, MINI_BH_MAX = 58, 230
OUTWARD = {"bottom": "below", "top": "above", "left": "left", "right": "right"}
FLIP = {"right": "left", "left": "right", "above": "below", "below": "above"}


def mini_layout(edge, side="right", bh=MINI_BH_MIN, bw=MINI_BW, valign=None, halign=None):
    if side == OUTWARD.get(edge):   # never point the box off the screen
        side = FLIP[side]
    valign = valign or {"bottom": "bottom", "top": "top"}.get(edge, "center")
    halign = halign or ("right" if edge == "right" else "left")
    return bubble_layout("mini", edge, side, th=MINI_TH, bw=bw, bh=bh, font=MINI_FONT,
                         valign=valign, halign=halign, btn_r=6)


def mini_variants(edge, side):
    """Ways to fit Mini Tux's box beside him on this edge, your choice first: the other
    side of him, or the box growing the other way along the edge."""
    if side == OUTWARD.get(edge):
        side = FLIP[side]
    if edge in ("top", "bottom"):
        if side in ("left", "right"):        # box beside him, along the edge
            return [(side, edge, None), (FLIP[side], edge, None)]
        return [(side, None, "left"), (side, None, "right")]   # box above/below him
    if side in ("above", "below"):            # left/right edge, box along it
        return [(side, None, edge), (FLIP[side], None, edge)]
    return [(side, "center", edge), (side, "bottom", edge), (side, "top", edge)]


def tux_offset(L, edge):
    """How far Tux's picture sits into the window along the edge he's on, and his length."""
    ax, ay, th = L.ART
    if edge in ("top", "bottom"):
        return ax, th * ART_W / ART_H
    return ay, th


def edge_overflow(L, edge, t, wa):
    """How many pixels of this layout's window would hang off the screen with Tux at t."""
    wx, wy, ww, wh = wa
    a, n = tux_offset(L, edge)
    start, length, size = (wx, ww, L.DW) if edge in ("top", "bottom") else (wy, wh, L.DH)
    pos = start + t * (length - n) - a
    return max(0, start - pos) + max(0, pos + size - (start + length))


FREE = free_layout()  # default; the real one follows your settings


URL = f"http://127.0.0.1:{PORT}"


# ---------------------------------------------------------------- model server
class Brain:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.req_lock = threading.Lock()   # one request at a time (prewarm vs. real question)

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
                # Spawn from the main thread: the "die with Tux" signal (PDEATHSIG) fires
                # when the *thread* that spawned the child exits, and worker threads are
                # short-lived, which killed the server mid-answer.
                done = threading.Event()

                def spawn():
                    self.proc = subprocess.Popen(
                        cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        preexec_fn=_child_setup)
                    done.set()
                    return False
                if threading.current_thread() is threading.main_thread():
                    spawn()
                else:
                    GLib.idle_add(spawn)
                    if not done.wait(10):
                        return False
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
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGKILL)
    except Exception:
        pass


# ---------------------------------------------------------------- knowledge: notes, Help, man
STOP = set("the a an to of in on for and or is how do i my what can with from it this that "
           "you me show get use using does make want need why when where which are be "
           "there not way all any some into out up its your have has will just everything "
           "working work problem issue help fix please but into than also only onto each "
           "every via per then other another".split())
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
    "bigger": "size large", "smaller": "size small", "larger": "size large",
    "zip": "archive compress zip", "unzip": "extract archive unzip", "battery": "battery power",
    "password": "password", "user": "user account", "sudo": "administrator root sudo",
}


def stem(w):
    for suf in ("ing", "ed", "s"):  # (no "es": it turned "updates" into "updat")
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
        self.help, self.man, self.man_names, self.summaries = [], [], set(), {}
        self.lib = Library()  # offline man pages / tldr / Arch Wiki (knowledge.py)
        try:
            idx = json.load(open(INDEX))
            for p in idx["help"]:
                p["tw"], p["dw"], p["bw"] = words(p["title"]), words(p["desc"]), words(p["words"])
                del p["words"]
                self.help.append(p)
            self.man = [(n, d, words(d)) for n, d in idx["man"]]
            self.man_names = {n.lower() for n, _, _ in self.man}
            self.summaries = {n: d for n, d in idx["man"]}
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
        names_cmd = any(name.lower() in raw and name.lower() not in STOP  # "make", "get"…
                        for _, name, _ in mans)
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
        if n == 1:
            return f"Pro tip: `{picks[0][0]}` → {picks[0][1]}"
        return "\n".join(f"• `{c}` → {d}" for c, d in picks)


SHELL_WORDS = set("cd export source . alias unalias history echo exit set unset type "
                  "sudo pushd popd jobs fg bg kill ulimit umask read wait for if while".split())


def recent_commands(n=RECENT_CMDS):
    """Last few terminal commands from bash history, minus anything secret-looking."""
    try:
        with open(HISTFILE, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            lines = f.read().decode("utf-8", "ignore").splitlines()[1:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line or line.startswith("#") or SECRETS.search(line) or line in out:
            continue
        first = line.split()[0]
        if first not in SHELL_WORDS and not shutil.which(first):
            continue  # not a command (e.g. text typed into some other program)
        out.append(line[:100])
        if len(out) == n:
            break
    return out[::-1]


# ---------------------------------------------------------------- offline library lookups
# words that are also command names but almost always mean the plain word in a question
COMMON_WORDS = set("file files time date free test install link more less last write users "
                   "groups join split sort which who what make get top watch screen service "
                   "display help info man open print read sleep stat true false yes see look "
                   "line lines size list show set run start stop update power sound keyboard "
                   "mouse network printer disk dir env id w from at as cd col tr wall".split())
REFS_MAX = 700    # characters of reference material per question (keeps answers quick)
# Feed library snippets to the model? Measured on the 1.5B model: answers were no better on
# average (some worse, ~2 s slower to start), so the library is used for instant tldr
# answers, source links and "more examples" instead. Worth turning on with a bigger model.
USE_LIBRARY_IN_PROMPT = False


def live_help(cmd, terms, max_lines=5):
    """The lines of `cmd --help` that match the question (well-behaved tools only)."""
    if cmd in NO_HELP or cmd.startswith("mkfs") or not shutil.which(cmd):
        return []
    try:
        r = subprocess.run([cmd, "--help"], capture_output=True, text=True, timeout=1.5,
                           stdin=subprocess.DEVNULL, env=dict(clean_env(), LC_ALL="C", PAGER="cat"))
    except (OSError, subprocess.TimeoutExpired):
        return []
    groups = _groups(terms)
    hits = []
    for line in (r.stdout or r.stderr)[:20000].splitlines():
        line = line.strip()
        if line.startswith("-") and _hits(line, groups):
            hits.append(re.sub(r"\s+", " ", line)[:110])
            if len(hits) >= max_lines:
                break
    return hits


# extra spellings a doc might use for a question word
DOC_SYNONYMS = {"folder": "directory", "folders": "directories", "changed": "modified",
                "change": "modify", "delete": "remove", "erase": "remove", "skip": "exclude",
                "ignore": "exclude", "biggest": "size", "largest": "size", "size": "sizes",
                "running": "process", "show": "list", "see": "list", "copy": "transfer",
                "download": "fetch", "hour": "minutes", "unzip": "extract", "untar": "extract",
                "computer": "host", "server": "host", "machine": "host", "pc": "host"}


def question_terms(q, man_names):
    """(command named in the question or None, the other meaningful words)."""
    raw = []
    for w in re.findall(r"[A-Za-z0-9][\w.+-]*", q):
        w = w.lower().strip(".")
        raw.append(w)
        if "." in w:  # tar.gz -> tar, gz
            raw += [p for p in w.split(".") if p]
    command = next((w for w in raw if w in man_names and w not in COMMON_WORDS and w not in STOP), None)
    terms = [w for w in raw if w not in STOP and len(w) > 1]
    other = [t for t in terms if t != command and t not in ("gz", "xz", "bz2")]
    return command, other


def _groups(terms):
    """One stem group per question word: the word and its doc spelling, 5-letter stems."""
    out = []
    for t in terms:
        if len(t) <= 2:
            continue
        g = {t[:5]}
        if t in DOC_SYNONYMS:
            g.add(DOC_SYNONYMS[t][:5])
        out.append(g)
    return out


def _hits(line, groups):
    low = re.sub(r"[\[\]]", "", line.lower())  # tldr writes E[x]tract
    return sum(1 for g in groups if any(st in low for st in g))


def _tldr_examples(lib, command, groups):
    """[(hits, example line)] from the command's own tldr page."""
    out = []
    for r in lib.search([command], command, limit=3):
        if r["source"] == "tldr" and r["title"] == command:
            for line in r["body"].splitlines():
                if line.startswith("- "):
                    h = _hits(line, groups)
                    if h:
                        out.append((h, line[2:]))
    out.sort(key=lambda e: -e[0])
    return out


def _tidy_tldr(desc, cmd, asked=()):
    desc = re.sub(r"\[(\w)\]", r"\1", desc)                      # E[x]tract -> Extract
    cmd = re.sub(r"\[(-[^|\]]+)\|[^\]]+\]", r"\1", cmd)          # [-i|--ignore-case] -> -i
    asked = set(asked)
    cmd = re.sub(r"(?<![\w/-])([\w-]+(?:\|[\w-]+)+)",            # start|stop|restart -> restart
                 lambda mm: next((w for w in mm.group(1).split("|") if w in asked),
                                 mm.group(1).split("|")[0]), cmd)
    return desc, cmd


def tldr_answer(lib, man_names, q):
    """An instant answer from tldr when one example clearly matches the question."""
    if not lib:
        return None
    command, other = question_terms(q, man_names)
    groups = _groups(other)
    if not command or not groups:
        return None
    ex = _tldr_examples(lib, command, groups)
    need = len(groups) if len(groups) <= 3 else -(-len(groups) * 3 // 4)
    if not ex or ex[0][0] < need:  # must cover (nearly) every word of the question
        return None
    m = re.match(r"(.*?):\s*`(.*)`\s*$", ex[0][1])
    if not m:
        return None
    desc, cmd = _tidy_tldr(m.group(1), m.group(2), other)
    desc = re.sub(r"\s*\([^)]*\)", "", desc)              # drop "(i.e. …)" asides
    if len(desc) > 55 and "," in desc:   # keep the first clause: "List the sizes of …"
        desc = desc.split(",", 1)[0]
    if len(desc) > 70:
        desc = desc[:70].rsplit(" ", 1)[0].rstrip(",;") + "…"
    return f"{desc}:\n```bash\n{cmd}\n```"


def library_refs(lib, man_names, q):
    """A few lines that directly answer the question, plus a link to the source.

    A 1.5B model copies whatever it's shown, so this is strict: matching tldr example lines,
    matching --help / man option lines, and at most one Arch Wiki snippet (general topics only).
    """
    command, other = question_terms(q, man_names)
    refs, link = [], None
    if not lib or not (other or command):
        return refs, link
    groups = _groups(other)
    if command:
        ex = _tldr_examples(lib, command, groups)
        if ex:
            refs.append(f"tldr {command}: " + " | ".join(e for _, e in ex[:2]))
        # option lines that match, from live --help, else from the man page
        opts = live_help(command, other, max_lines=3) if groups else []
        if not opts and groups:
            for r in lib.search(other or [command], command, limit=4):
                if r["source"] == "man" and r["title"] == command:
                    for m in re.finditer(r"(?m)^\s*(-{1,2}[\w][^\n]{0,110})", r["body"]):
                        if _hits(m.group(1), groups):
                            opts.append(re.sub(r"\s+", " ", m.group(1)))
            opts = opts[:3]
        if opts:
            refs.append(f"{command} options: " + " | ".join(opts))
        link = (f"man {command}", f"man:{command}")
    else:
        rows = [r for r in lib.search(other, None, limit=4) if r["source"] == "arch"]
        for r in rows:  # only if the page title or section is about the question
            if _hits(r["title"] + " " + r["heading"], groups):
                refs.append(f"ArchWiki (Arch Linux docs) '{r['title']} › {r['heading']}': "
                            + best_window(r["body"], other, 260))
                link = (f"ArchWiki: {r['title']}", r["ref"].split("#")[0])  # whole page
                break
    return refs, link


def build_messages(q, history, hp, mans, note, clip=None, recent=None, lib_refs=None):
    """System prompt + style examples + recent chat + question with its References."""
    refs = list(lib_refs or [])
    if hp:
        steps = " / ".join(hp["steps"][:2])
        refs.insert(0, f"GNOME Help '{hp['title']}': {hp['desc']} {steps}"[:260])
    refs += [f"man {n}: {d}" for n, d in mans]
    if note:
        refs.append(f"user's notes: {note[0]} : {note[1]}")
    kept, used = [], 0
    for r in refs:  # keep the prompt short so answers stay quick
        if used + len(r) > REFS_MAX:
            break
        kept.append(r)
        used += len(r)
    refs = kept
    user = q + ("\n\nReference:\n- " + "\n- ".join(refs) if refs else "")
    if recent:
        user += "\n\nRecent terminal commands (last = most recent):\n" + "\n".join(recent)
    if clip:
        user += "\n\nClipboard:\n```\n" + clip[:CLIP_MAX] + "\n```"
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
        elif line.startswith("📋 "):
            blocks.append(("clip", line))
        elif line.startswith("⌨ "):
            blocks.append(("ctx", line))
        elif line.startswith("▸ "):
            blocks.append(("action", line[2:].strip()))
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
        self.settings = load_settings()   # mode, edge, on_top, last free size/position
        self.x11 = X11.open() if USE_X11 else None
        self.L = FREE
        self.mini_drag = None             # Mini Tux being dragged along the edges
        self.anchored = None              # window size Mini Tux was last put on his edge at
        self.wiki_job = None              # "downloading the Arch Wiki"… while that runs
        self.placed_once = False
        self.brain = Brain()
        self.kb = Knowledge()
        self.art = cairo.ImageSurface.create_from_png(ART_PNG)
        self.art_runs = json.load(open(ART_JSON))["runs"]
        self.history = []
        self.bubble_text = ""          # greeting is set once the mode is known
        self.help_uri = None
        self.has_yelp = bool(shutil.which("yelp"))  # no Help app: 📖 links show a summary instead
        self.clip = None           # clipboard text attached to the next question
        self.clip_seen = None        # last clipboard text we looked at
        self.clip_primed = False     # startup clipboard is old news, not a question
        self.actions = {}            # "▸ label" lines in the bubble -> what clicking does
        self.last_copied = None      # what Tux itself copied (never fed back in)
        self.gen = 0
        self.idle_src = None
        self.hover_btns = False
        self.pointer_in = False      # mouse is over Tux or his bubble
        self.bubble_hidden = False   # tucked away after HIDE_SECS of nothing happening
        self.last_activity = time.monotonic()
        self.tick_src = GLib.timeout_add_seconds(1, self.tick)
        self.boxes = []              # [(x, y, w, h, kind, payload)] clickable bits of the bubble
        self.region_key = None
        self.free_home = None        # Free Tux's size and spot with the narrow box, while it's wide

        css = Gtk.CssProvider()
        style = """
            window, window.background { background: none; box-shadow: none; }
            entry.tux { border-radius: 14px; background: #ffffff; color: #111;
                        border: 2px solid #f2a900; min-height: 0; padding: 2px 8px; }
            entry.tux.dark { background: #24262b; color: #eeeeee; }
        """
        if hasattr(css, "load_from_string"):   # GTK 4.12+
            css.load_from_string(style)
        else:                                   # older GTK (Ubuntu 22.04, Debian 12)
            css.load_from_data(style, -1)
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
        rclick = Gtk.GestureClick(button=3)  # right-click the bubble: copy the whole answer
        rclick.connect("pressed", self.on_rclick)  # bubble: copy answer; Tux: options
        self.area.add_controller(rclick)
        drag = Gtk.GestureDrag(button=1)    # Mini Tux: slide along the screen edges
        drag.connect("drag-update", self.on_drag_update)
        drag.connect("drag-end", self.on_drag_end)
        self.area.add_controller(drag)
        # coming back to Tux (e.g. after copying an error): look at the clipboard
        self.connect("notify::is-active", lambda *_: self.is_active() and self.check_clipboard())

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.on_motion)
        motion.connect("leave", self.on_leave)
        self.area.add_controller(motion)

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self.on_scroll)
        self.add_controller(scroll)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

        self.connect("close-request", self.on_close)
        # once he's on screen: stay on top / take his spot (XWayland only)
        self.connect("map", lambda *_: GLib.timeout_add(150, self.place_window))
        # Free Tux's box can only fit its text once his window is up (it sizes the window)
        self.connect("map", lambda *_: GLib.timeout_add(250, lambda: self.fit_bubble() or False))
        if os.environ.get("TUX_TEST") and self.x11:  # test windows never take your keyboard
            self.connect("realize", lambda *_: self.no_focus_on_map())
        self.apply_mode()
        self.apply_theme()
        self.kb.lib.use_arch = self.opt("arch_wiki") and have_arch()  # opt-in add-on
        self.bubble_text = self.greeting()
        self.fit_bubble(force=True)

    def no_focus_on_map(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.get_surface().set_user_time(0)

    def greeting(self):
        keys = shortcut_get()
        how = f" ({shortcut_label(keys)} from anywhere)" if keys else ""
        return f"{self.kb.tip()}\nAsk me anything{how}. Right-click for settings."

    # ---- geometry
    def geom(self):
        w, h = self.area.get_width(), self.area.get_height()
        s = min(w / self.L.DW, h / self.L.DH) if w and h else 1.0
        return s, (w - self.L.DW * s) / 2, (h - self.L.DH * s) / 2

    def to_design(self, x, y):
        s, ox, oy = self.geom()
        return (x - ox) / s, (y - oy) / s

    def update_input_region(self):
        """Only Tux's outline, the bubble, buttons and entry take clicks."""
        surf = self.get_surface()
        if not surf:
            return
        s, ox, oy = self.geom()
        key = (self.L.mode, self.L.edge, round(s, 3), round(ox), round(oy), self.bubble_on(),
               self.entry.get_visible())
        if key == self.region_key:
            return
        self.region_key = key
        R = cairo.RectangleInt
        region = cairo.Region()
        ax, ay, ah = self.L.ART
        k = ah / self.art.get_height() * s
        for y, x0, x1 in self.art_runs:
            region.union(R(int(ox + ax * s + x0 * k), int(oy + ay * s + y * k),
                           int((x1 - x0) * k) + 1, int(2 * k) + 1))
        circles = [c for c in (self.L.GRIP,) if c]
        if self.buttons_on():
            circles += [self.L.BTN_MIN, self.L.BTN_CLOSE]
        for cx, cy, r in circles:
            r += 3
            region.union(R(int(ox + (cx - r) * s), int(oy + (cy - r) * s),
                           int(2 * r * s) + 1, int(2 * r * s) + 1))
        rects = [self.L.ENTRY] if self.entry.get_visible() else []
        if self.bubble_on():
            _side, b1, b2, tip = self.L.tail()
            xs, ys = (b1[0], b2[0], tip[0]), (b1[1], b2[1], tip[1])
            rects += [self.L.BUBBLE, (min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)]
        for x, y, w, h in rects:
            region.union(R(int(ox + x * s), int(oy + y * s), int(w * s) + 1, int(h * s) + 1))
        surf.set_input_region(region)

    # ---- drawing
    def draw(self, area, cr, w, h):
        s, ox, oy = self.geom()
        cr.save()
        cr.translate(ox, oy)
        cr.scale(s, s)
        ax, ay, ah = self.L.ART
        k = ah / self.art.get_height()
        cr.save()
        cr.translate(ax, ay)
        cr.scale(k, k)
        cr.set_source_surface(self.art, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        cr.restore()
        cr.restore()
        self.boxes = []
        if self.bubble_on():
            self.draw_bubble(cr, s, ox, oy)
        if self.L.GRIP or self.buttons_on():  # buttons in the bubble's corner, grip on his foot
            cr.save()
            cr.translate(ox, oy)
            cr.scale(s, s)
            self.draw_buttons(cr)
            cr.restore()
        self.place_entry(s, ox, oy)
        self.update_input_region()
        # Mini Tux: once the window has really changed size, put him back on his edge
        if self.L.mode == "mini" and self.x11 and (w, h) != self.anchored and not self.mini_drag:
            self.anchored = (w, h)
            GLib.idle_add(self.anchor_mini)

    def anchor_mini(self):
        if self.L.mode == "mini" and self.x11 and self.get_mapped() and not self.mini_drag:
            size = (self.get_width(), self.get_height())
            self.x11.move(self.xid(), *self.edge_pos(self.L.edge, self.settings.get("t", 0.95),
                                                     size=size))
        return False

    def buttons_on(self):
        return self.bubble_on()  # they live in the bubble's corner

    @staticmethod
    def circle(cr, cx, cy, r):
        cr.new_sub_path()
        cr.arc(cx, cy, r, 0, 2 * math.pi)

    def draw_buttons(self, cr):
        a = 1.0 if (self.hover_btns or not self.L.HOVER) else 0.6
        buttons = ((self.L.BTN_MIN, (0.98, 0.75, 0.15), "min"),
                   (self.L.BTN_CLOSE, (0.92, 0.30, 0.25), "close")) if self.buttons_on() else ()
        for (cx, cy, r), col, glyph in buttons:
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
        if self.L.GRIP:  # resize grip: three little lines on the right foot (Free Tux only)
            gx, gy, _ = self.L.GRIP
            cr.set_source_rgba(0.35, 0.2, 0, 0.7); cr.set_line_width(1.6)
            for d in (4, 8, 12):
                cr.move_to(gx + 6 - d, gy + 6); cr.line_to(gx + 6, gy + 6 - d)
            cr.stroke()

    def bubble_path(self, cr, X, Y, W, H, R, s, ox, oy):
        """Rounded box with the tail on whichever side faces Tux."""
        side, b1, b2, tip = self.L.tail()
        b1, b2, tip = [(ox + p[0] * s, oy + p[1] * s) for p in (b1, b2, tip)]
        cr.new_path()
        cr.move_to(X + R, Y)
        if side == "top":
            cr.line_to(*b1); cr.line_to(*tip); cr.line_to(*b2)
        cr.line_to(X + W - R, Y)
        cr.arc(X + W - R, Y + R, R, -math.pi / 2, 0)
        if side == "right":
            cr.line_to(*b1); cr.line_to(*tip); cr.line_to(*b2)
        cr.line_to(X + W, Y + H - R)
        cr.arc(X + W - R, Y + H - R, R, 0, math.pi / 2)
        if side == "bottom":
            cr.line_to(*b2); cr.line_to(*tip); cr.line_to(*b1)
        cr.line_to(X + R, Y + H)
        cr.arc(X + R, Y + H - R, R, math.pi / 2, math.pi)
        if side == "left":
            cr.line_to(*b2); cr.line_to(*tip); cr.line_to(*b1)
        cr.line_to(X, Y + R)
        cr.arc(X + R, Y + R, R, math.pi, 1.5 * math.pi)
        cr.close_path()

    THEMES = {
        "light": {"fill": (1, 1, 1), "edge": (0.1, 0.1, 0.12), "text": (0.08, 0.08, 0.1),
                  "cmd_bg": (0.13, 0.14, 0.17), "link": "#1a5fb4", "clip": "#8a4b00", "ctx": "#666666"},
        "dark": {"fill": (0.13, 0.14, 0.16), "edge": (0.55, 0.57, 0.62), "text": (0.93, 0.93, 0.95),
                 "cmd_bg": (0.05, 0.05, 0.06), "link": "#8ab4f8", "clip": "#f6c177", "ctx": "#a0a0a8"},
    }

    def theme(self):
        return self.THEMES.get(self.opt("theme"), self.THEMES["light"])

    def layout_blocks(self, cr, blocks, tw, size, s):
        """Pango layouts for each block at this font size -> (layouts, total height)."""
        th_ = self.theme()
        layouts, total = [], 0
        for kind, txt in blocks:
            lay = PangoCairo.create_layout(cr)
            fam = "Monospace" if kind == "cmd" else "Sans"
            lay.set_font_description(Pango.FontDescription.from_string(f"{fam} {max(size, 5):.1f}"))
            lay.set_width(int((tw - (10 * s if kind == "cmd" else 0)) * Pango.SCALE))
            lay.set_wrap(Pango.WrapMode.WORD_CHAR)
            if kind == "cmd":
                lay.set_text(txt, -1)
            elif kind == "help":
                lay.set_markup(f"<span foreground='{th_['link']}'><u>{esc(txt)}</u></span>", -1)
            elif kind == "clip":
                lay.set_markup(f"<span foreground='{th_['clip']}'>{esc(txt)}</span>", -1)
            elif kind == "ctx":
                lay.set_markup(f"<span foreground='{th_['ctx']}'><small>{esc(txt)}</small></span>", -1)
            elif kind == "action":
                lay.set_markup(f"<span foreground='{th_['link']}'><b>▸ {esc(txt)}</b></span>", -1)
            else:
                lay.set_markup(inline_markup(txt), -1)
            lh = lay.get_pixel_size()[1] + (8 * s if kind == "cmd" else 0)
            layouts.append((kind, txt, lay, lh))
            total += lh + 4 * s
        return layouts, total

    def draw_bubble(self, cr, s, ox, oy):
        th_ = self.theme()
        bx, by, bw, bh = self.L.BUBBLE
        X, Y, W, H, R = ox + bx * s, oy + by * s, bw * s, bh * s, 16 * s
        self.bubble_path(cr, X, Y, W, H, R, s, ox, oy)
        alpha = self.opt("opacity")          # see-through setting; the text stays solid
        cr.set_source_rgba(*th_["fill"], alpha); cr.fill_preserve()
        cr.set_source_rgba(*th_["edge"], max(0.35, 0.85 * alpha))
        cr.set_line_width(max(1.5, 2 * s)); cr.stroke()

        pad = 12 * s
        top = self.L.TEXT_TOP * s   # room for the buttons in the corner
        Y, H = Y + top, H - top
        tw, th = W - 2 * pad, H - 2 * pad
        if self.entry.get_visible():  # the text box sits inside the bubble, at the bottom
            th -= (self.L.ENTRY[3] + 6) * s
        blocks = parse_answer(self.bubble_text) or [("text", self.bubble_text)]
        size = self.L.FONT * self.opt("text_size") * s
        while True:  # shrink text until it fits
            layouts, total = self.layout_blocks(cr, blocks, tw, size, s)
            if total <= th or size <= 6.5 * s:
                break
            size *= 0.9
        y = Y + pad
        cr.save()
        cr.rectangle(X + pad / 2, Y + pad / 2, W - pad, H - pad); cr.clip()
        for kind, txt, lay, lh in layouts:
            if kind == "cmd":
                cr.set_source_rgb(*th_["cmd_bg"])
                self.round_rect(cr, X + pad, y, tw, lh, 6 * s); cr.fill()
                cr.set_source_rgb(0.55, 0.95, 0.55)
                cr.move_to(X + pad + 5 * s, y + 4 * s)
                self.boxes.append((X + pad, y, tw, lh, "cmd", txt))
            else:
                if kind == "help":
                    self.boxes.append((X + pad, y, tw, lh, "help", self.help_uri))
                elif kind == "clip":
                    self.boxes.append((X + pad, y, tw, lh, "clip", None))
                elif kind == "action":
                    self.boxes.append((X + pad, y, tw, lh, "action", txt))
                cr.set_source_rgb(*th_["text"])
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
        x, y, w, h = self.L.ENTRY
        self.entry.set_margin_start(int(ox + x * s))
        self.entry.set_margin_top(int(oy + y * s))
        self.entry.set_size_request(int(w * s), int(h * s))

    # ---- input
    @staticmethod
    def inside_circle(dx, dy, c, extra=2):
        return math.hypot(dx - c[0], dy - c[1]) <= c[2] + extra

    def on_motion(self, _ctl, x, y):
        dx, dy = self.to_design(x, y)
        hv = self.L.HOVER
        self.set_hover(bool(hv) and hv[0] < dx < hv[2] and hv[1] < dy < hv[3])
        self.pointer_in = True
        self.touch()

    def on_leave(self, *_):
        self.pointer_in = False
        self.set_hover(False)

    # ---- bubble tucks away when nothing's happening, so an on-top Tux stays out of the way
    def bubble_on(self):
        return bool(self.bubble_text) and not self.bubble_hidden

    def touch(self):
        """Something happened: show the bubble and restart the hide countdown."""
        self.last_activity = time.monotonic()
        if self.bubble_hidden:
            self.bubble_hidden = False
            self.region_key = None
            self.fit_bubble()
            self.area.queue_draw()
        if not self.tick_src:
            self.tick_src = GLib.timeout_add_seconds(1, self.tick)

    def tick(self):
        if self.pointer_in or self.entry.get_text():
            self.last_activity = time.monotonic()
        secs = self.opt("hide_secs")
        if not secs:  # "never": keep the bubble up
            self.tick_src = None
            return False
        if time.monotonic() - self.last_activity < secs:
            return True
        self.bubble_hidden = True
        if not self.entry.get_text():
            self.entry.set_visible(False)
        self.fit_bubble()
        self.region_key = None
        self.area.queue_draw()
        self.tick_src = None
        return False  # stop ticking until something happens

    def set_hover(self, v):
        if v != self.hover_btns:
            self.hover_btns = v
            self.area.queue_draw()

    def on_press(self, gesture, n_press, x, y):
        self.touch()
        dx, dy = self.to_design(x, y)
        dev, btn, t = gesture.get_device(), gesture.get_current_button(), gesture.get_current_event_time()
        surf = self.get_surface()
        if self.buttons_on() and self.inside_circle(dx, dy, self.L.BTN_CLOSE):
            self.close(); return
        if self.buttons_on() and self.inside_circle(dx, dy, self.L.BTN_MIN):
            self.minimize(); return
        if self.L.GRIP and self.inside_circle(dx, dy, self.L.GRIP, 4):
            self.free_home = None   # you're resizing him: that's his new size
            surf.begin_resize(Gdk.SurfaceEdge.SOUTH_EAST, dev, btn, x, y, t); return
        for bx, by, bw, bh, kind, payload in self.boxes:
            if bx <= x <= bx + bw and by <= y <= by + bh:
                if kind == "cmd":
                    self.copy(payload)
                elif kind == "clip":
                    self.drop_clip()
                elif kind == "action":
                    fn = self.actions.get(payload)
                    if fn:
                        fn()
                elif payload:
                    self.open_help(payload)
                return
        b = self.L.BUBBLE
        if self.bubble_on() and b[0] <= dx <= b[0] + b[2] and b[1] <= dy <= b[1] + b[3]:
            if not self.history:  # still on the tip screen: show another tip
                self.say(self.greeting())
            return
        if self.L.mode == "mini":
            if self.x11:  # Mini Tux: click = ask, drag = slide along the screen edges
                self.mini_drag = {"start": self.x11.pointer(), "moved": False}
            else:         # no XWayland: can't place ourselves, let GNOME move him
                surf.begin_move(dev, btn, x, y, t)
            return
        cx, cy, rx, ry = self.L.BELLY
        if math.hypot((dx - cx) / rx, (dy - cy) / ry) <= 1 and n_press == 1:
            self.show_entry(); return
        self.free_home = None   # you're moving him: wherever he lands is his new spot
        surf.begin_move(dev, btn, x, y, t)

    # ---- Mini Tux rides the screen edges
    def on_drag_update(self, *_):
        d = self.mini_drag
        if not d or not self.x11:
            return
        px, py = self.x11.pointer()
        if not d["moved"]:
            if abs(px - d["start"][0]) + abs(py - d["start"][1]) < 6:
                return
            d["moved"] = True
            d["wa"] = self.x11.workarea()
        wx, wy, ww, wh = d["wa"]
        # the edge nearest the pointer, and how far along it (Tux's middle under the pointer)
        dist = {"left": px - wx, "right": wx + ww - px, "top": py - wy, "bottom": wy + wh - py}
        edge = min(dist, key=dist.get)
        tw = MINI_TH * ART_W / ART_H
        if edge in ("top", "bottom"):
            t = (px - tw / 2 - wx) / max(1, ww - tw)
        else:
            t = (py - MINI_TH / 2 - wy) / max(1, wh - MINI_TH)
        self.settings["edge"], self.settings["t"] = edge, min(1.0, max(0.0, t))
        self.set_layout(self.mini_shape(self.L.BUBBLE[3], self.L.BUBBLE[2], d["wa"]))
        self.x11.move(self.xid(), *self.edge_pos(edge, self.settings["t"], d["wa"]))

    def on_drag_end(self, *_):
        d, self.mini_drag = self.mini_drag, None
        if not d:
            return
        if d["moved"]:
            save_settings(self.settings)
        else:
            self.show_entry()  # just a click on Mini Tux: ask him something

    def edge_pos(self, edge, t, wa=None, size=None):
        """Where Mini Tux's window goes. t (0..1 along the edge) is where Tux himself sits,
        so his box can grow and shrink beside him while he stays put; the window only
        slides if the box would otherwise hang off the screen."""
        wx, wy, ww, wh = wa or self.x11.workarea()
        W, H = size or (self.L.DW, self.L.DH)
        a, n = tux_offset(self.L, edge)
        if edge in ("top", "bottom"):
            x = min(max(wx + t * (ww - n) - a, wx), wx + ww - W)
            return x, (wy + wh - H if edge == "bottom" else wy)
        y = min(max(wy + t * (wh - n) - a, wy), wy + wh - H)
        return (wx if edge == "left" else wx + ww - W), y

    def xid(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return self.get_surface().get_xid()

    # ---- settings (right-click) values, with defaults
    DEFAULTS = {"mode": "free", "bubble_side": "right", "opacity": 0.97, "text_size": 1.0,
                "theme": "light", "hide_secs": HIDE_SECS, "on_top": False, "arch_wiki": False}

    def opt(self, key):
        return self.settings.get(key, self.DEFAULTS[key])

    # ---- the two modes
    def apply_mode(self):
        self.free_home = None
        if self.opt("mode") == "mini":
            self.set_layout(self.mini_shape())
            self.fit_bubble(force=True)
        else:
            self.L = free_layout(self.opt("bubble_side"))
            w, h = self.settings.get("free_size") or free_default_size(self.L)
            aspect = self.L.DW / self.L.DH
            if not h or abs(w / h - aspect) > 0.25 * aspect:  # saved from another layout
                w, h = free_default_size(self.L)
            self.set_default_size(int(w), int(h))
        self.region_key = None
        self.area.queue_draw()
        if self.get_mapped():
            GLib.timeout_add(150, self.place_window)

    def set_layout(self, L):
        """Switch to a new Mini Tux layout and resize his window to match."""
        if (L.DW, L.DH, L.BUBBLE, L.SIDE, L.ART) != (self.L.DW, self.L.DH, self.L.BUBBLE,
                                                      self.L.SIDE, self.L.ART):
            self.region_key = None
            self.area.queue_draw()
        self.L = L
        self.set_default_size(math.ceil(L.DW), math.ceil(L.DH))

    def mini_shape(self, bh=MINI_BH_MIN, bw=MINI_BW, wa=None):
        """Mini Tux's layout at this box size. The box goes on the side you picked, unless
        it wouldn't fit there at its biggest, where Tux sits; then it goes the other way,
        so Tux never has to move for it."""
        edge = self.settings.get("edge", "bottom")
        variants = mini_variants(edge, self.opt("bubble_side"))
        if self.x11:
            wa = wa or self.x11.workarea()
            t = self.settings.get("t", 0.95)
            big = (MINI_BH_MAX, self.widest_box(1.0, MINI_FONT))
            variants.sort(key=lambda v: edge_overflow(mini_layout(edge, v[0], *big, *v[1:]),
                                                      edge, t, wa))   # stable: your pick wins ties
        return mini_layout(edge, variants[0][0], bh, bw, *variants[0][1:])

    def measure(self, s=1.0, font=None):
        """Text measuring at the size Tux draws it (s = Free Tux's scale): a context, the
        font size and the answer's blocks."""
        cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
        blocks = parse_answer(self.bubble_text) or [("text", self.bubble_text)]
        return cr, (font or self.L.FONT) * self.opt("text_size") * s, blocks

    @staticmethod
    def mono_width(cr, text, size):
        lay = PangoCairo.create_layout(cr)
        lay.set_font_description(Pango.FontDescription.from_string(f"Monospace {max(size, 5):.1f}"))
        lay.set_text(text, -1)
        return lay.get_pixel_size()[0]

    def widest_box(self, s, font=None):
        """The widest the box gets, in design units: a CMD_FIT-character command fits."""
        cr, size, _ = self.measure(s, font)
        return self.mono_width(cr, "0" * CMD_FIT, size) / s + CMD_PAD

    def wanted_box(self, s=1.0):
        """Box size (width, height) for what Tux is saying. Narrow and short when he's idle;
        wider when a command would wrap (up to widest_box()), and taller when there's more
        text, so it never has to shrink."""
        mini = self.L.mode == "mini"
        narrow, short, tall = ((MINI_BW, MINI_BH_MIN, MINI_BH_MAX) if mini else
                               (FREE_BW, free_layout(self.L.SIDE).BUBBLE[3], FREE_BH_MAX))
        if not self.bubble_on():
            return narrow, short
        cr, size, blocks = self.measure(s)
        widest = self.widest_box(s)
        cmds = [txt for kind, txt in blocks if kind == "cmd"]
        bw = narrow
        if cmds:
            longest = max(self.mono_width(cr, c, size) for c in cmds) / s + CMD_PAD
            bw = max(narrow, min(widest, longest))

        def need(w):  # box height the text needs at this width
            return self.text_height(w - 24, s) + self.box_pad()

        h = need(bw)
        if h > tall and bw < widest and need(widest) < h:
            bw, h = widest, need(widest)   # a long answer: wider beats tiny or cut-off text
        return math.ceil(bw), int(min(tall, max(short, h)))

    def box_pad(self):
        """Box height that isn't text: margins, the button row, the text box if it's open."""
        return 24 + self.L.TEXT_TOP + (self.L.ENTRY[3] + 6 if self.entry.get_visible() else 0)

    def fit_bubble(self, force=False):
        """The box fits its message: wider for a long command, taller for more text. It
        shrinks back when Tux goes idle."""
        if self.L.mode == "free":
            self.fit_free(force)
            return
        bw, bh = self.wanted_box()
        old = self.L.BUBBLE
        if not force and abs(bh - old[3]) < 4 and abs(bw - old[2]) < 4:
            return
        self.set_layout(self.mini_shape(bh, bw))
        if self.x11 and self.get_mapped() and not self.mini_drag:  # stay on the edge
            self.x11.move(self.xid(), *self.edge_pos(self.L.edge, self.settings.get("t", 0.95)))

    def fit_free(self, force=False):
        """Free Tux: his window grows beside him to fit the answer and goes back after, to
        exactly the size and spot you gave him. Tux stays put; the window only slides if the
        box would run off the screen."""
        if not self.get_mapped() or min(self.get_default_size()) <= 0:
            return
        side = self.L.SIDE
        narrow = free_layout(side)
        home = self.free_home or self.free_home_now()
        s = min(home["w"] / narrow.DW, home["h"] / narrow.DH)
        bw, bh = self.wanted_box(s)
        if not self.x11:  # can't move the window to keep Tux put: only grow away from him
            if free_layout(side, bw, bh).ART[1] != narrow.ART[1]:
                bh = narrow.BUBBLE[3]
            if free_layout(side, bw, bh).ART[0] != narrow.ART[0]:
                bw = FREE_BW
        old = self.L.BUBBLE
        if not force and abs(bw - old[2]) < 4 and abs(bh - old[3]) < 4:
            return
        new = free_layout(side, bw, bh)
        nw = home["w"] + math.ceil((new.DW - narrow.DW) * s)
        nh = home["h"] + math.ceil((new.DH - narrow.DH) * s)
        grown = new.BUBBLE != narrow.BUBBLE
        self.L = new
        self.region_key = None
        self.set_default_size(nw, nh)
        self.area.queue_draw()
        if self.x11:   # Tux stays put
            x = home["x"] + round((narrow.ART[0] - new.ART[0]) * s)
            y = home["y"] + round((narrow.ART[1] - new.ART[1]) * s)
            if grown:  # ...unless the box would run off the screen
                wx, wy, ww, wh = self.x11.workarea()
                x, y = max(wx, min(x, wx + ww - nw)), max(wy, min(y, wy + wh - nh))
            self.x11.move(self.xid(), x, y)
        self.free_home = home if grown else None

    def free_home_now(self):
        """Free Tux's size and spot with the narrow box, worked out from how he is now."""
        w, h = self.get_default_size()
        narrow = free_layout(self.L.SIDE)
        s = min(w / self.L.DW, h / self.L.DH)
        x, y = self.x11.pos(self.xid()) if self.x11 else (None, None)
        if x is not None:
            x = round(x + (self.L.ART[0] - narrow.ART[0]) * s)
            y = round(y + (self.L.ART[1] - narrow.ART[1]) * s)
        return {"w": round(w - (self.L.DW - narrow.DW) * s),
                "h": round(h - (self.L.DH - narrow.DH) * s), "x": x, "y": y}

    def text_height(self, width, s=1.0):
        """How tall the current bubble text is, in design units, at this box text width."""
        cr, size, blocks = self.measure(s)
        return self.layout_blocks(cr, blocks, width * s, size, s)[1] / s

    def place_window(self):
        """On top / position. Needs XWayland; without it Tux is a normal window."""
        if not self.x11 or not self.get_mapped():
            return False
        xid = self.xid()
        mini = self.L.mode == "mini"
        self.x11.set_on_top(xid, mini or self.settings.get("on_top", False))
        if mini:
            self.x11.move(xid, *self.edge_pos(self.L.edge, self.settings.get("t", 0.95)))
        elif not self.placed_once and self.settings.get("free_pos"):
            self.x11.move(xid, *self.settings["free_pos"])
        self.placed_once = True
        return False

    def set_mode(self, mode):
        self.remember_free_spot()
        self.settings["mode"] = mode
        save_settings(self.settings)
        self.apply_mode()
        if mode == "mini":
            self.say("Mini Tux! I stay on top, on every workspace. Drag me along any screen edge; "
                     "click me to ask. Right-click for settings.")
        else:
            self.say("Free Tux! Drag me anywhere. Resize with the lines on my foot or Ctrl+scroll.")

    def toggle_on_top(self, on=None):
        self.settings["on_top"] = (not self.settings.get("on_top")) if on is None else on
        save_settings(self.settings)
        self.place_window()
        self.say("I'll stay on top of your windows, on every workspace." if self.settings["on_top"]
                 else "Okay, I'm a normal window again.")

    def remember_free_spot(self):
        """Save Free Tux's size and spot as they are with the narrow box."""
        if self.L.mode == "free" and self.get_mapped():
            home = self.free_home or self.free_home_now()
            self.settings["free_size"] = [home["w"], home["h"]]
            if self.x11:
                self.settings["free_pos"] = [home["x"], home["y"]]

    def on_scroll(self, ctl, _dx, dy):
        if self.L.mode == "mini" or not (ctl.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK):
            return False
        self.free_home = None
        h = self.get_height()
        nh = max(110, min(320, int(h * (0.9 if dy > 0 else 1.1))))  # wide layout: keep it sane
        self.set_default_size(int(nh * self.L.DW / self.L.DH), nh)
        return True

    def on_key(self, _ctl, keyval, _code, state):
        self.touch()
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and keyval in (Gdk.KEY_q, Gdk.KEY_Q, Gdk.KEY_w):
            self.close(); return True
        if ctrl and keyval in (Gdk.KEY_c, Gdk.KEY_C):
            cmd = first_command(self.bubble_text)
            if cmd:
                self.copy(cmd)
            return True
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
            self.check_clipboard()
        self.entry.set_visible(True)
        self.fit_bubble()
        self.region_key = None
        self.area.queue_draw()
        self.entry.grab_focus()
        if prefix:
            self.entry.set_text(prefix)
            self.entry.set_position(-1)

    # ---- the keyboard shortcut (tux.py --toggle)
    def toggle(self):
        """Not focused (or tucked away): call Tux up. Already focused: put him away."""
        if self.is_active() and not self.minimized():
            self.put_away()
        else:
            self.summon()

    def minimized(self):
        surf = self.get_surface()
        return bool(surf and surf.get_state() & Gdk.ToplevelState.MINIMIZED)

    def summon(self):
        """Up front, bubble showing, text box open and ready to type."""
        self.present()
        if self.x11:  # once he's on screen, make sure he gets the keyboard
            GLib.timeout_add(80, lambda: (self.get_mapped() and self.x11.activate(self.xid()),
                                          False)[-1])
        self.touch()
        self.show_entry()

    def put_away(self):
        """Hand the keyboard back to whatever you were using: he minimizes, like the yellow
        button. The shortcut brings him straight back."""
        if not self.entry.get_text():
            self.entry.set_visible(False)
            self.region_key = None
        self.minimize()

    def copy(self, text):
        self.last_copied = text
        self.get_clipboard().set(text)
        if "(copied" not in self.bubble_text:
            self.say(self.bubble_text.rstrip() + "\n(copied ✓)")

    def copy_answer(self):
        """Whole answer as plain text: no ``` fences, no Help link."""
        lines = [s for k, s in parse_answer(self.bubble_text) if k in ("text", "cmd")]
        text = "\n".join(l for l in lines if l != "(copied ✓)").strip()
        if text:
            self.copy(text)

    def check_clipboard(self):
        self.get_clipboard().read_text_async(None, self.on_clipboard)

    def on_clipboard(self, cb, res):
        try:
            text = (cb.read_text_finish(res) or "").strip()
        except GLib.Error:
            return
        if not self.clip_primed:  # first look at startup: note what's there, don't offer it
            self.clip_primed = True
            self.clip_seen = text
            return
        self.accept_clip(text)

    def accept_clip(self, text):
        if not text or text == self.clip_seen or text == self.last_copied or len(text) > 6000:
            return
        self.clip_seen = text
        self.clip = text
        snippet = " ".join(text.split())[:48] + ("…" if len(text) > 48 else "")
        self.say(f"📋 {snippet}  ✕\nHit Enter to explain it, or ask \"what's this?\"")
        if not self.entry.get_visible():
            self.show_entry()

    def drop_clip(self):
        self.clip = None
        self.say("Clipboard dropped. Ask away.")

    # ---- options + guided setups (clickable "▸" lines in the bubble)
    def say_actions(self, text, actions):
        self.actions = dict(actions)
        self.say(text + "".join(f"\n▸ {label}" for label, _ in actions))

    def on_rclick(self, _gesture, _n, x, y):
        self.touch()
        dx, dy = self.to_design(x, y)
        bx, by, bw, bh = self.L.BUBBLE
        if self.bubble_on() and bx <= dx <= bx + bw and by <= dy <= by + bh and \
                not self.bubble_text.startswith("Settings"):
            self.copy_answer()
        else:
            self.show_settings()

    # ---- Settings (right-click): click a line to cycle its value; it applies right away
    SEE_THROUGH = [(0.97, "solid"), (0.8, "a little"), (0.6, "half"), (0.4, "glass")]
    TEXT_SIZES = [(0.85, "small"), (1.0, "normal"), (1.2, "large")]
    HIDE_AFTER = [(10, "10 s"), (20, "20 s"), (60, "1 min"), (0, "never")]
    SIDE_NAMES = {"right": "right of Tux", "left": "left of Tux", "above": "above Tux",
                  "below": "below Tux"}

    @staticmethod
    def _label(options, value):
        return min(options, key=lambda o: abs(o[0] - value))[1]

    def _cycle(self, key, values):
        cur = self.opt(key)
        i = min(range(len(values)), key=lambda j: abs(values[j] - cur) if isinstance(cur, (int, float))
                else (0 if values[j] == cur else 1))
        self.settings[key] = values[(i + 1) % len(values)]
        save_settings(self.settings)

    def show_settings(self, page=1):
        mini = self.opt("mode") == "mini"

        def change(key, values, relayout=False):
            def go():
                self._cycle(key, values)
                if relayout:
                    self.remember_free_spot()
                    self.apply_mode()
                self.apply_theme()
                self.show_settings(page)
            return go

        if page == 1:
            items = [
                (f"Mode: {'Mini Tux (tiny, on the screen edge)' if mini else 'Free Tux (move & resize)'}",
                 lambda: (self.set_mode("free" if mini else "mini"), self.show_settings(1))),
                (f"Bubble: {self.SIDE_NAMES[self.opt('bubble_side')]}",
                 change("bubble_side", list(BUBBLE_SIDES), relayout=True)),
                (f"See-through: {self._label(self.SEE_THROUGH, self.opt('opacity'))}",
                 change("opacity", [v for v, _ in self.SEE_THROUGH])),
                (f"Text size: {self._label(self.TEXT_SIZES, self.opt('text_size'))}",
                 change("text_size", [v for v, _ in self.TEXT_SIZES], relayout=mini)),
                ("More settings…", lambda: self.show_settings(2)),
            ]
        else:
            on_top = "always (Mini Tux)" if mini else ("on" if self.opt("on_top") else "off")
            items = [
                (f"Theme: {self.opt('theme')}", change("theme", ["light", "dark"])),
                (f"Hide bubble after: {self._label(self.HIDE_AFTER, self.opt('hide_secs'))}",
                 change("hide_secs", [v for v, _ in self.HIDE_AFTER])),
                (f"Keep on top: {on_top}",
                 (lambda: self.show_settings(2)) if mini else
                 (lambda: (self.toggle_on_top(), self.show_settings(2)))),
                (f"Keyboard shortcut: {self.shortcut_state()}", self.shortcut_offer),
                (f"Arch Wiki: {self.wiki_state()}", self.wiki_offer),
                ("‹ Back", lambda: self.show_settings(1)),
            ]
        note = "" if self.x11 else " (on-top & edges need XWayland)"
        self.say_actions(f"Settings {page}/2{note}", items)

    def apply_theme(self):
        if self.opt("theme") == "dark":
            self.entry.add_css_class("dark")
        else:
            self.entry.remove_css_class("dark")
        self.region_key = None
        self.area.queue_draw()

    # ---- the keyboard shortcut that calls Tux from anywhere (a GNOME custom shortcut)
    def shortcut_state(self):
        keys = shortcut_get()
        return "how to add one" if keys is None else shortcut_label(keys) if keys else "off"

    def shortcut_offer(self):
        keys = shortcut_get()
        back = [("‹ Back to settings", lambda: self.show_settings(2))]
        if keys is None:
            self.say_actions("This desktop isn't GNOME, so add it in its keyboard settings: "
                             f"a shortcut that runs\n```bash\n{TOGGLE_CMD}\n```", back)
            return
        ok, msg = shortcut_set(not keys)
        if not ok:
            self.say_actions(f"{msg} To pick other keys: {SHORTCUT_HOW}", back)
        elif keys:  # it was on, now it's off
            self.show_settings(2)
        else:
            self.say_actions(f"Press {shortcut_label(shortcut_get())} from anywhere to call me "
                             "up, ready to type. Press it again to put me away. Change the keys "
                             "in Settings → Keyboard → View and Customize Shortcuts → Custom "
                             "Shortcuts.", back)

    # ---- the optional offline Arch Wiki (download / switch on / off / delete)
    def wiki_state(self):
        if self.wiki_job:
            return self.wiki_job
        if self.opt("arch_wiki") and have_arch():
            return "on"
        return "off (downloaded)" if have_arch() else "off"

    def wiki_offer(self):
        if self.wiki_job:
            self.say(f"Still {self.wiki_job}. Keep using me; I'll say when it's done.")
        elif self.opt("arch_wiki") and have_arch():
            self.say_actions("The offline Arch Wiki is on.", [
                ("Turn it off (keep the files)", lambda: self.set_wiki(False)),
                ("Turn it off and delete it (frees ~130 MB)",
                 lambda: self.wiki_job_run(["remove-arch"], "deleting the Arch Wiki", False)),
                ("‹ Back", lambda: self.show_settings(2))])
        elif have_arch():
            self.set_wiki(True)  # already downloaded: switch it on instantly
        else:
            self.say_actions(
                "Add the Arch Wiki to my offline library? Deep guides on boot, audio, networking "
                "and more. 19 MB download, ~130 MB on disk.", [
                    ("Download it (in the background)",
                     lambda: self.wiki_job_run(["add-arch"], "downloading the Arch Wiki", True)),
                    ("Not now", lambda: self.show_settings(2))])

    def set_wiki(self, on):
        self.settings["arch_wiki"] = on
        save_settings(self.settings)
        self.kb.lib.use_arch = on and have_arch()
        self.show_settings(2)

    def wiki_job_run(self, args, what, enable):
        """Download+add or delete the wiki at low priority, then reopen the rebuilt library."""
        self.wiki_job = what
        self.say(f"Okay, {what}… Keep using me; I'll say when it's done.")
        cmd = ["nice", "-n", "15", sys.executable, os.path.join(HERE, "knowledge.py")] + args
        if shutil.which("ionice"):
            cmd = ["ionice", "-c3"] + cmd

        def work():
            try:
                ok = subprocess.run(cmd, capture_output=True, timeout=1800, env=clean_env(),
                                    stdin=subprocess.DEVNULL).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
            GLib.idle_add(finish, ok)

        def finish(ok):
            self.wiki_job = None
            if ok:
                self.settings["arch_wiki"] = enable
                save_settings(self.settings)
            self.kb.lib = Library(use_arch=self.opt("arch_wiki"))  # the index was rebuilt
            if not ok:
                self.say("That didn't work (no internet?). Try again from Settings later.")
            elif enable:
                self.say("Arch Wiki added ✓ It's in my offline library now.")
            else:
                self.say("Arch Wiki deleted ✓ (freed ~130 MB).")
            return False

        threading.Thread(target=work, daemon=True).start()

    def tuck_bubble(self):
        self.last_activity = 0
        self.pointer_in = False

    def on_top_offer(self):
        """'stay on top' / 'sit on my dock': offer the two ways to stay visible."""
        if not self.x11:
            self.say("Staying on top needs XWayland, which isn't running here. Try your window "
                     "menu (often Alt+Space) → Always on Top.")
            return
        self.say_actions("I can stay visible two ways:", [
            ("Mini Tux: tiny, rides the screen edge", lambda: self.set_mode("mini")),
            ("Big Tux, kept on top of other windows",
             lambda: (self.set_mode("free") if self.L.mode == "mini" else None,
                      self.toggle_on_top(True))),
            ("Not now", lambda: self.say("No problem. Right-click me any time.")),
        ])

    # ---- 📖 links: open in Help (yelp) if it's there, else show the gist in the bubble
    def open_help(self, uri):
        if not uri.startswith("file://") and not self.has_yelp:
            self.has_yelp = bool(shutil.which("yelp"))  # maybe installed since startup
            if not self.has_yelp:
                self.help_summary(uri)
                return
        opener = "xdg-open" if uri.startswith("file://") else "yelp"  # Arch Wiki → browser
        try:
            subprocess.Popen([opener, uri], start_new_session=True, env=clean_env(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.say(f"Couldn't open that page ({opener} is missing).")

    def help_summary(self, uri):
        prev_text, prev_actions = self.bubble_text, dict(self.actions)
        if uri.startswith("man:"):
            name = uri[4:].split("(")[0]
            desc = next((d for n, d, _ in self.kb.man if n == name), "")
            text = f"{name}: {desc}\nRead the full page in a terminal:\n`man {name}`"
        else:
            p = next((p for p in self.kb.help if p["uri"] == uri), None)
            if not p:
                text = "I can't find that Help page anymore."
            else:
                lines = [p["title"]] + ([p["desc"]] if p["desc"] else [])
                if p["steps"]:
                    lines += [f"{i}. {s}" for i, s in enumerate(p["steps"], 1)]
                elif p["text"]:
                    lines.append(p["text"])
                text = "\n".join(lines)

        def back():
            self.actions = prev_actions
            self.say(prev_text)
        self.say_actions(text, [("Get the Help app for full pages", self.yelp_install_info),
                                ("Back", back)])

    def yelp_install_info(self):
        """Tux never runs anything: give the command for this machine's package manager."""
        for tool, cmd in (("apt", "sudo apt install yelp"), ("dnf", "sudo dnf install yelp"),
                          ("pacman", "sudo pacman -S yelp"), ("zypper", "sudo zypper install yelp")):
            if shutil.which(tool):
                self.say(f"Install GNOME Help (click to copy):\n`{cmd}`\n"
                         "Then click a 📖 link again. No restart needed.")
                return
        self.say("Install the package called yelp (GNOME Help) with your package manager, "
                 "then click a 📖 link again.")

    def say(self, text):
        if bool(text) != bool(self.bubble_text):
            self.region_key = None
        self.bubble_text = text
        self.touch()
        self.fit_bubble()
        self.area.queue_draw()
        return False

    # ---- asking
    def on_submit(self, entry):
        q = entry.get_text().strip()
        entry.set_text("")
        clip, self.clip = self.clip, None
        if not q and not clip:
            return
        asked = q
        refers = bool(REFERS.search(q)) or not q
        if clip and not refers:
            clip = None  # you asked about something else: leave the clipboard out of it
        search_text = q
        if not q:
            q = "Explain this. If it's an error, how do I fix it?"
            search_text = clip[:300]
        self.gen += 1
        if self.idle_src:
            GLib.source_remove(self.idle_src); self.idle_src = None
        cmd = self.command_in(asked, clip)
        if cmd:  # "what would this command do?": a breakdown from the docs; it's never run
            self.say("reading the docs…")
            threading.Thread(target=self.explain_flow, args=(cmd, self.gen), daemon=True).start()
            return
        if ON_TOP_WORDS.search(q) and not clip:  # "stay on top" / "sit on my dock"
            self.on_top_offer()
            return
        target = None if clip else install_target(q)
        if target:  # install / remove / "is X installed": check this machine's real packages
            self.say("checking apt & snap…")
            threading.Thread(target=self.install_flow, args=(q, self.gen) + target,
                             daemon=True).start()
            return
        if TIP_WORDS.search(q) and not clip:  # tips come from the curated list, instantly, no model
            self.say("Power moves:\n" + self.kb.tip(3))
            return
        hp, mans, note = self.kb.search(search_text + (" " + clip[:300] if clip and search_text == q else ""))
        self.help_uri = hp["uri"] if hp else None
        quick = None if (clip or refers) else self.kb.quick(q)
        if quick:  # hand-checked answer: instant, no model
            self.say(quick + (f"\n📖 {hp['title']} ›" if hp else ""))
            return
        tl = None if (clip or refers) else tldr_answer(self.kb.lib, self.kb.man_names, q)
        if tl:  # a tldr example that clearly matches: instant, and checked by the tldr project
            self.say(tl)
            return
        # instant answer from your notes while the model thinks
        recent = recent_commands() if refers and not clip else None
        ctx = []
        if clip:
            ctx.append("📋 reading your clipboard")
        if recent:
            ctx.append(f"⌨ using your last {len(recent)} terminal commands")
        pre = "\n".join(ctx) + ("\n" if ctx else "")
        self.say(pre + (f"From your notes:\n`{note[0]}`\n\nthinking…" if note else "thinking…"))
        threading.Thread(target=self.ask, args=(q, self.gen, hp, mans, note, clip, recent),
                         daemon=True).start()

    def command_in(self, q, clip):
        """The shell command you want explained, or None.

        A command typed or pasted on its own ("tar -xzf a.tgz"), asked about ("what does
        `ls -la` do?"), or copied and then Enter / "what does this do?"."""
        if clip and (not q or (EXPLAIN_ASK.search(q) and not re.search(
                r"\b(why|fail|failed|error|fix|wrong)\b", q, re.I))):
            c = clip.strip()
            c = re.sub(r"^\$\s+", "", c)
            if looks_like_command(c, self.kb.man_names, explicit=True):
                return c
        if not q:
            return None
        c, explicit = strip_ask(q)
        if explicit and not looks_like_command(c, self.kb.man_names, explicit=True):
            return None
        return c if looks_like_command(c, self.kb.man_names, explicit=explicit) else None

    def explain_flow(self, cmd, gen):
        """Worker thread: break the command down from this machine's docs, then let the model
        add a one-line summary on top. Nothing is ever run."""
        def show(text):
            if gen == self.gen:
                GLib.idle_add(self.say, text)

        try:
            b = breakdown(cmd, self.kb.lib, self.kb.summaries)
        except Exception as e:  # odd input shouldn't take Tux down
            show(f"Couldn't break that down: {e}")
            return
        shown = cmd if len(cmd) <= 40 else cmd[:39] + "…"
        head = f"What `{shown}` would do:"
        body = as_text(b) + "\n(Not run: I only read the docs.)"
        if not (os.path.exists(MODEL) and os.path.exists(SERVER_BIN)):
            show(head + "\n" + body)
            return
        show(head + "\nthinking…\n" + body)
        if not self.brain.ensure(lambda m: None):
            show(head + "\n" + body)
            return
        parts = "\n".join("- " + l.replace("`", "") for l in b["lines"])
        msgs = [{"role": "system", "content": EXPLAIN_PROMPT}]
        for uq, ua in EXPLAIN_FEWSHOT:
            msgs += [{"role": "user", "content": uq}, {"role": "assistant", "content": ua}]
        msgs.append({"role": "user", "content": f"{cmd}\n\nParts:\n{parts}"})

        def tidy(t):
            t = re.sub(r"```.*", "", t, flags=re.S).strip()
            return t.split("\n")[0].strip() if t else ""

        try:
            out = self.chat(msgs, 50, gen, lambda t: show(head + "\n" + (tidy(t) or "thinking…")
                                                           + "\n" + body))
        except Exception:
            out = ""
        if out is None:  # you asked something else meanwhile
            return
        summary = tidy(out)
        if "only know Linux" in summary:
            summary = ""
        show(head + ("\n" + summary if summary else "") + "\n" + body)
        GLib.idle_add(self.arm_idle_timer, gen)

    def chat(self, msgs, max_tokens, gen, on_text):
        """Stream an answer from the local model; on_text(text so far) ~20×/s.
        Returns the whole text, or None if a newer question took over. Raises on errors."""
        body = json.dumps({"messages": msgs, "stream": True, "max_tokens": max_tokens,
                           "temperature": 0.2, "cache_prompt": True}).encode()
        req = urllib.request.Request(URL + "/v1/chat/completions", body,
                                     {"Content-Type": "application/json"})
        out = ""
        self.brain.req_lock.acquire()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                last = 0.0
                for raw in r:
                    if gen != self.gen:
                        return None
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
                        on_text(out)
        finally:
            self.brain.req_lock.release()
        return out

    def install_flow(self, q, gen, verb, name):
        """Worker thread: answer from apt/snap if we can, otherwise ask the model."""
        try:
            ans = install_answer(verb, name)
        except Exception:
            ans = None
        if ans:
            if gen == self.gen:
                GLib.idle_add(self.say, ans)
            return
        hp, mans, note = self.kb.search(q)
        self.help_uri = hp["uri"] if hp else None
        # tell the model what we already know, so it doesn't invent an apt command
        known = (f"\n(I checked this computer: there's no package called '{name}' in apt or the "
                 "Snap Store. Suggest where to get it, e.g. the vendor's site, a .deb, or Flatpak.)")
        self.ask(q + known, gen, hp, mans, note)

    def ask(self, q, gen, hp, mans, note, clip=None, recent=None):
        def status(msg):
            if gen == self.gen:
                GLib.idle_add(self.say, msg)

        if not os.path.exists(MODEL) or not os.path.exists(SERVER_BIN):
            status("My brain isn't installed yet (model or llama-server missing).")
            return
        if not self.brain.ensure(status):
            status("Couldn't start my brain (llama-server). Try again?")
            return
        lib_refs, link = library_refs(self.kb.lib, self.kb.man_names, q)
        msgs = build_messages(q, self.history, hp, mans, note, clip, recent,
                              lib_refs if USE_LIBRARY_IN_PROMPT else None)
        footer = f"\n📖 {hp['title']} ›" if hp else ""
        if not hp and link and link[1].startswith("file://"):  # Arch Wiki page (if it's on)
            footer, self.help_uri = f"\n📖 {link[0]} ›", link[1]
        try:
            out = self.chat(msgs, MAX_TOKENS + (40 if clip or recent else 0), gen, status)
        except Exception as e:
            status(f"Oops, my brain hiccuped: {e}")
            return
        if out is None:
            return
        out = out.strip()
        if recent and "only know Linux" in out:
            # the model can't see command output; be straight about it instead of refusing
            out = (f"Last command: `{recent[-1]}`\nI can't see its output. "
                   "Copy the error and click me for an exact fix.")
        status(out if "only know Linux" in out else out + footer)
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
        if not self.brain.req_lock.acquire(blocking=False):
            return  # a real question is already running
        try:
            urllib.request.urlopen(urllib.request.Request(
                URL + "/v1/chat/completions", body, {"Content-Type": "application/json"}),
                timeout=60).read()
        except Exception:
            pass
        finally:
            self.brain.req_lock.release()
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
        self.remember_free_spot()
        save_settings(self.settings)
        self.brain.stop()
        return False


class App(Gtk.Application):
    def __init__(self):
        # TUX_TEST=1: separate instance for tests, so it never hijacks your open Tux
        test = bool(os.environ.get("TUX_TEST"))
        super().__init__(application_id="local.tux.helper" + (".test" if test else ""),
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option("toggle", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Call Tux up ready to type, or put him away (for a keyboard "
                             "shortcut)", None)

    def do_command_line(self, cmdline):
        """Runs in the Tux that's already open (a second `tux.py` just passes its options
        along), or in a new one if none is."""
        win = self.get_active_window()
        if cmdline.get_options_dict().contains("toggle"):
            if win:
                win.toggle()
            else:  # not running yet: start up ready to type
                self.activate()
                self.get_active_window().summon()
        else:
            self.activate()
        return 0

    def do_activate(self):
        if not os.path.exists(INDEX):  # first run: build the Help/man index
            subprocess.run(["python3", os.path.join(HERE, "setup_tux.py")])
        win = self.get_active_window() or Tux(self)
        win.present()
        # test hooks: TUX_ASK="question" auto-asks, TUX_SNAPSHOT=file.png saves a render
        if os.environ.get("TUX_CLIP"):  # test hook: pretend this was just copied
            GLib.timeout_add(300, lambda: (win.accept_clip(os.environ["TUX_CLIP"]), False)[-1])
        if os.environ.get("TUX_ASK") is not None:
            GLib.timeout_add(500, lambda: (win.entry.set_text(os.environ["TUX_ASK"]),
                                           win.on_submit(win.entry), False)[-1])
        # TUX_CLICKS="options|Set it up": open options, then click "▸" lines by label
        for i, label in enumerate(filter(None, os.environ.get("TUX_CLICKS", "").split("|"))):
            def click(label=label):
                if label == "options":
                    win.show_options()
                elif label in win.actions:
                    win.actions[label]()
                else:
                    print("no such option:", label, list(win.actions), flush=True)
                return False
            GLib.timeout_add(700 + 400 * i, click)
        if os.environ.get("TUX_PROBE"):  # test hook: report how the window really ended up
            def probe():
                d = Gdk.Display.get_default()
                info = {"backend": type(d).__name__, "composited": d.is_composited(),
                        "rgba": d.is_rgba(), "mode": win.L.mode, "edge": win.L.edge,
                        "size": (win.get_width(), win.get_height())}
                if win.x11:
                    xid = win.xid()
                    info["pos"] = win.x11.pos(xid)
                    info["workarea"] = win.x11.workarea()
                    t, f, n, a, p = (ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(),
                                     ctypes.c_ulong(), ctypes.c_void_p())
                    win.x11.X.XGetWindowProperty(win.x11.dpy, xid, win.x11.STATE, 0, 32, 0, 4,
                                                 ctypes.byref(t), ctypes.byref(f), ctypes.byref(n),
                                                 ctypes.byref(a), ctypes.byref(p))
                    st = ([ctypes.cast(p, ctypes.POINTER(ctypes.c_ulong))[i] for i in range(n.value)]
                          if p.value else [])
                    info["on_top"] = win.x11.ABOVE in st
                    info["all_workspaces"] = win.x11.STICKY in st
                print("probe", json.dumps(info), flush=True)
                return False
            GLib.timeout_add(int(os.environ["TUX_PROBE"]), probe)
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
    if sys.argv[1:2] == ["--shortcut"]:  # set up / remove the keyboard shortcut, then exit
        sys.exit(shortcut_cli(sys.argv[2:]))
    if sys.argv[1:2] == ["--explain"]:  # what would this command do? (never runs it)
        import explain
        sys.exit(explain.main(sys.argv[2:]))
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.exit(App().run(sys.argv))
