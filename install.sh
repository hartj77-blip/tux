#!/usr/bin/env bash
# Tux installer: no sudo needed. Downloads llama.cpp (CPU build, ~25 MB) and the
# model (~1 GB), builds the Help/man index, and adds Tux to your app grid.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
TUX_DIR="$PWD"
MODEL_URL="https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF/resolve/main/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"
MODEL="models/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"

say() { printf '\033[1;33m🐧 %s\033[0m\n' "$*"; }

# 1. system bits (stock Ubuntu GNOME already has these)
[ "$(uname -m)" = x86_64 ] || { echo "Sorry, Tux's installer only supports x86_64 for now."; exit 1; }
if ! python3 -c "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk; import cairo" 2>/dev/null; then
    echo "Missing GTK4 Python bits. Install them with:"
    echo "  sudo apt install python3-gi gir1.2-gtk-4.0 python3-cairo"
    exit 1
fi
command -v curl >/dev/null || { echo "Please install curl:  sudo apt install curl"; exit 1; }

# 2. llama.cpp CPU build (newest release that has one)
if ! ls bin/*/llama-server >/dev/null 2>&1; then
    say "Downloading llama.cpp…"
    url=$(curl -fsSL "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15" | python3 -c '
import json, re, sys
for rel in json.load(sys.stdin):
    for a in rel["assets"]:
        if re.fullmatch(r"llama-b\d+-bin-ubuntu-x64\.tar\.gz", a["name"]):
            print(a["browser_download_url"]); sys.exit()')
    [ -n "$url" ] || { echo "Couldn't find a llama.cpp Linux build on GitHub. Try again later."; exit 1; }
    mkdir -p bin
    curl -fL --progress-bar "$url" | tar xz -C bin
fi

# 3. the model (resumes if interrupted)
if [ ! -f "$MODEL" ]; then
    say "Downloading Tux's brain (~1 GB)…"
    mkdir -p models
    curl -fL -C - --progress-bar -o "$MODEL.part" "$MODEL_URL"
    mv "$MODEL.part" "$MODEL"
fi

# 4. Help + man page index
say "Indexing GNOME Help and man pages…"
python3 setup_tux.py

# 5. app grid entry
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$apps"
sed "s|@TUX_DIR@|$TUX_DIR|g" tux.desktop.in > "$apps/tux.desktop"

# 6. optional: save bash history after every command, so Tux can see what you just ran
MARK="# tux: save history after every command"
if [ -t 0 ] && ! grep -qF "$MARK" ~/.bashrc 2>/dev/null; then
    read -rp "Let Tux see your recent terminal commands (adds one line to ~/.bashrc)? [y/N] " yn
    if [[ "$yn" =~ ^[Yy] ]]; then
        cp ~/.bashrc ~/.bashrc.tux-backup
        printf '\n%s\nPROMPT_COMMAND="history -a${PROMPT_COMMAND:+; $PROMPT_COMMAND}"\n' "$MARK" >> ~/.bashrc
        echo "  Added (backup: ~/.bashrc.tux-backup). Applies to new terminals."
    fi
fi

# 7. optional: Tux Perch GNOME extension: on top of other windows, standing on the dock
UUID="tux-perch@hartj77-blip"
if [ -t 0 ] && command -v gnome-extensions >/dev/null; then
    read -rp "Keep Tux on top of other windows, standing on your dock (GNOME extension)? [Y/n] " yn
    if [[ ! "$yn" =~ ^[Nn] ]]; then
        ext="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"
        mkdir -p "$ext"
        cp "extension/$UUID/"* "$ext/"
        # GNOME only discovers new extensions at login, so also add it to the enabled list
        gnome-extensions enable "$UUID" 2>/dev/null || python3 - "$UUID" <<'PY'
import ast, subprocess, sys
uuid = sys.argv[1]
cur = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"],
                     capture_output=True, text=True).stdout.strip().removeprefix("@as ")
exts = ast.literal_eval(cur or "[]")
if uuid not in exts:
    subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", str(exts + [uuid])])
PY
        echo "  Installed. Log out and back in once and Tux will stand on your dock."
    fi
fi

say "Done! Open Tux from your app grid, or run:  python3 $TUX_DIR/tux.py"
