#!/usr/bin/env bash
# Tux uninstaller: no sudo needed. Undoes what install.sh did, then asks before
# deleting the Tux folder itself (it holds the ~1 GB model).
set -euo pipefail

main() {
    cd "$(dirname "$(readlink -f "$0")")"
    local TUX_DIR="$PWD"
    say() { printf '\033[1;33m🐧 %s\033[0m\n' "$*"; }

    # 1. stop Tux and his brain if they're running
    pkill -f "$TUX_DIR/tux.py" 2>/dev/null && echo "  Closed Tux." || true
    pkill -f "$TUX_DIR/bin/.*llama-server" 2>/dev/null || true

    # 2. app grid entry
    local desktop="${XDG_DATA_HOME:-$HOME/.local/share}/applications/tux.desktop"
    [ -f "$desktop" ] && rm -f "$desktop" && echo "  Removed Tux from the app grid."

    # 3. settings (mode, theme, where you left him…)
    local conf="${XDG_CONFIG_HOME:-$HOME/.config}/tux"
    [ -d "$conf" ] && rm -rf "$conf" && echo "  Removed Tux's settings."

    # 4. the optional ~/.bashrc line (only the lines Tux added; the rest is untouched)
    local MARK="# tux: save history after every command"
    if grep -qF "$MARK" ~/.bashrc 2>/dev/null; then
        cp ~/.bashrc ~/.bashrc.tux-uninstall-backup
        python3 -I - "$MARK" <<'EOF'
import os, sys
mark, path = sys.argv[1], os.path.expanduser("~/.bashrc")
lines = open(path).read().split("\n")
i = lines.index(mark)
end = i + 2 if i + 1 < len(lines) and lines[i + 1].startswith('PROMPT_COMMAND="history -a') else i + 1
start = i - 1 if i > 0 and not lines[i - 1].strip() else i  # the blank line the installer added
open(path, "w").write("\n".join(lines[:start] + lines[end:]))
EOF
        echo "  Removed Tux's line from ~/.bashrc (backup: ~/.bashrc.tux-uninstall-backup)."
    fi

    # 5. the Tux folder itself: ask first
    local yn=n
    if [ -t 0 ]; then
        read -rp "Also delete the Tux folder ($TUX_DIR, ~1 GB with the model)? [y/N] " yn
    fi
    if [[ "$yn" =~ ^[Yy] ]]; then
        cd /
        rm -rf "$TUX_DIR"
        say "Tux is gone. Bye! 👋"
    else
        say "Tux is uninstalled. His folder is still at $TUX_DIR. Delete it any time, or run ./install.sh to bring him back."
    fi
}

main "$@"  # everything above is read before anything runs, so deleting the folder is safe
exit
