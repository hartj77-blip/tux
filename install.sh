#!/usr/bin/env bash
# Tux installer: no sudo needed. Downloads llama.cpp (CPU build, ~25 MB) and the
# model (~1 GB), builds the Help/man index, and adds Tux to your app grid.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
TUX_DIR="$PWD"
MODEL_URL="https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF/resolve/main/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"
MODEL="models/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"

say() { printf '\033[1;33m🐧 %s\033[0m\n' "$*"; }

# 1. system bits (stock Ubuntu GNOME already has these): check everything, then report once
[ "$(uname -m)" = x86_64 ] || { echo "Sorry, Tux's installer only supports x86_64 for now."; exit 1; }
command -v python3 >/dev/null || { echo "Tux needs Python 3. Install python3 with your package manager."; exit 1; }

# package names differ per distro: gtk, curl, yelp, pillow, man
if   command -v apt-get >/dev/null; then pm="sudo apt install"
    p_gtk="python3-gi gir1.2-gtk-4.0 python3-cairo python3-gi-cairo"; p_curl=curl; p_yelp=yelp; p_pil=python3-pil; p_man=man-db
elif command -v dnf >/dev/null; then pm="sudo dnf install"
    p_gtk="python3-gobject gtk4 python3-cairo"; p_curl=curl; p_yelp=yelp; p_pil=python3-pillow; p_man=man-db
elif command -v pacman >/dev/null; then pm="sudo pacman -S --needed"
    p_gtk="python-gobject gtk4 python-cairo"; p_curl=curl; p_yelp=yelp; p_pil=python-pillow; p_man=man-db
elif command -v zypper >/dev/null; then pm="sudo zypper install"
    p_gtk="python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 python3-pycairo python3-gobject-cairo"; p_curl=curl
    p_yelp=yelp; p_pil=python3-Pillow; p_man=man
else pm=""
    p_gtk="PyGObject + GTK 4 + pycairo for Python 3"; p_curl=curl; p_yelp=yelp; p_pil=Pillow; p_man=man-db
fi

need=() need_pkgs=() opt=() opt_pkgs=()
python3 -c "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk; import cairo; gi.require_foreign('cairo')" 2>/dev/null ||
    { need+=("GTK 4 for Python: Tux's window"); need_pkgs+=($p_gtk); }
command -v curl >/dev/null || { need+=("curl: downloads Tux's brain"); need_pkgs+=($p_curl); }
command -v yelp >/dev/null || { opt+=("yelp (GNOME Help): opens full Help pages from 📖 links; without it Tux shows a summary"); opt_pkgs+=($p_yelp); }
command -v man >/dev/null || { opt+=("man pages: Tux learns every command's options from them"); opt_pkgs+=($p_man); }
python3 -c "import PIL" 2>/dev/null || { opt+=("Pillow: only for using your own Tux picture"); opt_pkgs+=($p_pil); }

if [ ${#need[@]} -gt 0 ] || [ ${#opt[@]} -gt 0 ]; then
    if [ ${#need[@]} -gt 0 ]; then
        echo "Required (Tux can't install without these):"
        printf '  ✗ %s\n' "${need[@]}"
    fi
    if [ ${#opt[@]} -gt 0 ]; then
        echo "Optional: what each adds:"
        printf '  • %s\n' "${opt[@]}"
    fi
    echo
    if [ -n "$pm" ]; then
        [ ${#need[@]} -gt 0 ] && echo "Install the required ones:  $pm ${need_pkgs[*]}"
        [ ${#opt[@]} -gt 0 ] && echo "Add the optional ones:      $pm ${opt_pkgs[*]}"
    else
        echo "Install them with your distro's package manager."
    fi
    if [ ${#need[@]} -gt 0 ]; then
        echo "Then run ./install.sh again."
        exit 1
    fi
    echo "(Continuing without the optional ones. You can add them any time.)"
    echo
fi

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

# 2b. make sure llama.cpp actually runs here (before the big model download)
server=$(ls bin/*/llama-server | sort | head -n1)
if ! out=$("$server" --version 2>&1); then
    echo "llama.cpp (Tux's brain engine) won't run on this system:"
    missing=$(ldd "$server" 2>/dev/null | awk '/not found/ {print "  ✗ " $1}' | sort -u || true)
    if [ -n "$missing" ]; then
        echo "Missing libraries:"; echo "$missing"
        echo "These come with newer systems (Ubuntu 22.04+, Debian 12+, Fedora 36+)."
        echo "libssl.so.3 is OpenSSL 3; libgomp.so.1 is part of GCC's runtime (libgomp1 on Ubuntu)."
    else
        echo "$out" | tail -n 5 | sed 's/^/  /'
    fi
    exit 1
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

# 4b. offline Linux library: full man pages + tldr examples. The Arch Wiki is an optional
#     add-on (Tux: right-click → Settings → More settings → Arch Wiki); kept fresh if you have it.
arch=""
[ -d knowledge/arch ] && arch="--arch"
say "Building Tux's offline library (man pages, tldr${arch:+, Arch Wiki})…"
nice -n 15 python3 knowledge.py build $arch || echo "  (library build failed; Tux still works without it)"

# 5. app grid entry
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$apps"
sed "s|@TUX_DIR@|$TUX_DIR|g" tux.desktop.in > "$apps/tux.desktop"

# 6. keyboard shortcut that calls Tux from anywhere (a GNOME custom shortcut; never overrides
#    keys something else already uses). Change or remove it any time: right-click Tux → Settings.
shortcut=$(python3 tux.py --shortcut status 2>/dev/null || true)
case "$shortcut" in
    on*)  python3 tux.py --shortcut on | sed 's/^/  /' || true ;;  # keeps your keys, points it here
    off)  if [ -t 0 ]; then
              read -rp "Call Tux from anywhere with Ctrl+Alt+Space (press again to put him away)? [Y/n] " yn
              [[ "$yn" =~ ^[Nn] ]] || python3 tux.py --shortcut on | sed 's/^/  /' || true
          fi ;;
    *)    echo "  Tip: in your desktop's keyboard settings, add a shortcut that runs:"
          echo "       python3 $TUX_DIR/tux.py --toggle" ;;
esac

# 7. optional: save bash history after every command, so Tux can see what you just ran
MARK="# tux: save history after every command"
if [ -t 0 ] && ! grep -qF "$MARK" ~/.bashrc 2>/dev/null; then
    read -rp "Let Tux see your recent terminal commands (adds one line to ~/.bashrc)? [y/N] " yn
    if [[ "$yn" =~ ^[Yy] ]]; then
        cp ~/.bashrc ~/.bashrc.tux-backup
        printf '\n%s\nPROMPT_COMMAND="history -a${PROMPT_COMMAND:+; $PROMPT_COMMAND}"\n' "$MARK" >> ~/.bashrc
        echo "  Added (backup: ~/.bashrc.tux-backup). Applies to new terminals."
    fi
fi

say "Done! Open Tux from your app grid, or run:  python3 $TUX_DIR/tux.py"
echo "   Want him tiny on the screen edge, see-through, or kept on top? Right-click Tux → Settings."
