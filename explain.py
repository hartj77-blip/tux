#!/usr/bin/env python3
"""Tux's "what would this command do?" breakdown. Nothing here ever runs the command.

Each part gets its meaning from this machine's own docs: the man page summary for the
command, the matching option lines from `--help` or the man page for each flag, plus plain
words for pipes and redirects. Risky things (deleting, wiping drives, running as root,
piping the internet into a shell…) get a ⚠ line from fixed rules, not from the model.

Terminal:  python3 explain.py 'rm -rf ~/old'
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# never run these, even with --help (Tux reads their man page instead)
NO_HELP = set("reboot shutdown poweroff halt init telinit rm dd shred wipefs fdisk sfdisk cfdisk "
              "parted gparted kill killall pkill sudo su login passwd yes xdg-open logout "
              "gnome-session-quit mkswap swapoff swapon format reset startx xinit Xorg X xkill "
              "chvt systemctl loginctl eject rfkill".split())
# --help only for real programs installed in the system folders, never a script you point at
SAFE_DIRS = ("/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/", "/usr/local/bin/", "/snap/bin/")
WRAPPERS = {"sudo": "runs the rest as administrator (root)", "doas": "runs the rest as root",
            "nohup": "keeps it running after the terminal closes", "time": "times how long it takes",
            "nice": "runs it at lower priority", "env": "runs it with changed settings",
            "watch": "reruns it every 2 seconds", "xargs": "runs it on each line it's given"}
OPERATORS = {"|": "sends that output into", "|&": "sends that output (and errors) into",
             "&&": "then, only if that worked:", "||": "if that failed, instead:",
             ";": "then:", "&": "in the background", ">": "writes the output to",
             ">>": "adds the output to the end of", "<": "reads its input from",
             "2>": "writes the errors to", "2>>": "adds the errors to", "&>": "writes output and errors to",
             "2>&1": "mixes errors into the output"}
# what a subcommand does, for tools where the man page doesn't put it on one line
SUBCOMMANDS = {
    "apt": {"install": "installs packages", "remove": "uninstalls packages (keeps their settings)",
            "purge": "uninstalls packages and their settings", "update": "refreshes the package lists",
            "upgrade": "installs available updates", "full-upgrade": "installs updates, removing packages if needed",
            "autoremove": "removes packages nothing needs any more", "search": "searches for packages",
            "show": "shows package details", "list": "lists packages"},
    "systemctl": {"start": "starts a service", "stop": "stops a service", "restart": "restarts a service",
                  "reload": "reloads a service's settings", "status": "shows a service's state and recent log",
                  "enable": "starts a service at every boot", "disable": "stops it starting at boot",
                  "mask": "blocks a service from starting at all", "unmask": "unblocks a service",
                  "daemon-reload": "rereads changed service files", "--failed": "lists broken services"},
    "snap": {"install": "installs a snap", "remove": "uninstalls a snap", "refresh": "updates snaps",
             "list": "lists installed snaps", "find": "searches the Snap Store"},
    "flatpak": {"install": "installs an app", "uninstall": "uninstalls an app", "update": "updates apps",
                "list": "lists installed apps", "run": "starts an app"},
    "git": {"clone": "downloads a repository", "status": "shows what changed", "add": "stages changes",
            "commit": "saves staged changes as a commit", "push": "uploads your commits",
            "pull": "downloads and merges new commits", "fetch": "downloads new commits, no merging",
            "checkout": "switches branch or restores files", "switch": "switches branch",
            "reset": "moves the branch back / unstages", "clean": "deletes untracked files",
            "stash": "sets your changes aside", "log": "shows the history", "diff": "shows the changes",
            "branch": "lists or makes branches", "merge": "joins another branch in", "rebase": "replays your commits on top"},
    "docker": {"run": "starts a new container", "ps": "lists running containers", "stop": "stops a container",
               "rm": "deletes a container", "rmi": "deletes an image", "pull": "downloads an image",
               "build": "builds an image", "exec": "runs a command inside a container",
               "system": "manages Docker itself (e.g. prune: delete unused data)"},
    "nmcli": {"radio": "turns radios (Wi-Fi…) on or off", "dev": "shows / controls network devices",
              "device": "shows / controls network devices", "con": "manages saved connections",
              "connection": "manages saved connections"},
}
# fallbacks when this machine has no docs for them (rm/dd/kill are never run with --help)
BUILTIN_FLAGS = {
    "rm": {"-r": "deletes folders and everything inside", "-R": "deletes folders and everything inside",
           "--recursive": "deletes folders and everything inside", "-f": "never asks, ignores missing files",
           "--force": "never asks, ignores missing files", "-i": "asks before each file",
           "-I": "asks once before deleting many", "-v": "says what it deletes", "-d": "removes empty folders"},
    "find": {"-name": "matches the file name (case-sensitive)", "-iname": "matches the name, any case",
             "-type": "only files (f) or folders (d)", "-mtime": "changed N days ago (+N: more than N)",
             "-mmin": "changed N minutes ago (-N: less than N)", "-size": "by size (+100M: bigger than 100 MB)",
             "-delete": "deletes what it finds", "-exec": "runs a command on each thing it finds",
             "-maxdepth": "how many folders deep to look", "-empty": "only empty files/folders",
             "-user": "owned by that user", "-perm": "by permissions", "-newer": "changed after that file",
             "-print": "prints the matches", "-path": "matches the whole path"},
    "kill": {"-9": "force-kills (SIGKILL), no cleanup", "-KILL": "force-kills, no cleanup",
             "-15": "asks it to quit (the default)", "-l": "lists signal names"},
    "dd": {"if": "reads from `{}`", "of": "writes to `{}`", "bs": "moves {} at a time",
           "status": "shows how far along it is", "count": "copies only {} blocks",
           "conv": "conversion options", "oflag": "output options (e.g. sync)"},
}
SHELL_BUILTINS = {"cd": "changes folder", "export": "sets a variable for this shell and its programs",
                  "source": "runs a file's commands in this shell", ".": "runs a file's commands in this shell",
                  "alias": "makes a shortcut name for a command", "history": "your past commands",
                  "exit": "closes this shell", "unset": "removes a variable", "type": "says what a name is",
                  "read": "reads a line of input", "exec": "replaces the shell with the command",
                  "eval": "runs the text as a command", "set": "changes shell options", "ulimit": "sets resource limits",
                  "umask": "sets default permissions for new files", "jobs": "lists background jobs",
                  "fg": "brings a job to the front", "bg": "resumes a job in the background"}
SYSTEM_PATHS = r"(/|/\*|/(bin|boot|dev|etc|lib|lib64|opt|proc|root|sbin|srv|sys|usr|var)(/\*?)?|~/?|~/\*|\$HOME/?|\*|\.|\.\.|/home/?)"


# ---------------------------------------------------------------- spotting a command
def strip_ask(q):
    """'what does `ls -la` do?' -> 'ls -la'; anything else comes back as it is."""
    q = q.strip()
    bt = re.search(r"`([^`]+)`", q)
    if bt and not re.search(r"\$\(|`[^`]*`[^`]*`", q.replace(bt.group(0), "", 1)):
        return bt.group(1).strip(), True
    m = re.match(r"(?i)\s*(?:explain|break ?down|dry[- ]?run|what (?:does|would|will|did)|"
                 r"what(?:'s| is)|whats|is it safe to (?:run|use)|is (?:this|it) safe:?|"
                 r"should i run|what happens if i (?:run|type))\s*:?\s+(?:the command\s+)?(.+)$", q)
    if m:
        rest = re.sub(r"(?i)\s+(?:do|does|mean|means|actually do)?\s*\??$", "", m.group(1)).strip()
        return rest.strip("'\""), True
    return re.sub(r"^\$\s+", "", q), False


def looks_like_command(s, known=None, explicit=False):
    """Is `s` a shell command rather than a question? `known`: man page names (optional).
    explicit: you asked "what does … do?", so a lone command name ("grep") counts too."""
    s = s.strip()
    if not s or "\n" in s.strip() or len(s) > 400:
        return False
    try:
        toks = split(s)
    except ValueError:
        return False
    words_ = [t for t in toks if t not in OPERATORS]
    if not words_:
        return False
    first = words_[0]
    while first in WRAPPERS and len(words_) > 1 or re.fullmatch(r"\w+=\S*", first) and len(words_) > 1:
        words_ = words_[1:]
        first = words_[0]
    script = bool(re.fullmatch(r"(\.{1,2}|~)?/[\w./+-]+", first))   # ./install.sh, ~/bin/x
    if not script and not re.fullmatch(r"[\w.+-]+", first):
        return False
    is_cmd = script or first in SHELL_BUILTINS or first in ("history", "alias") \
        or (known is not None and first.lower() in known) or bool(shutil.which(first))
    if not is_cmd:
        return False
    rest = words_[1:]
    # "find big files" is a question; "find . -name x" / "cat /etc/fstab" / "ls | wc" are commands
    shellish = script or any(t.startswith("-") for t in rest) or any(t in OPERATORS for t in toks) \
        or any(re.search(r"[/~*=$]|^\.\.?$|\.\w{1,4}$", t) for t in rest) or toks[0] in WRAPPERS
    if explicit and not rest:
        return True
    return shellish and not s.rstrip().endswith("?")


# ---------------------------------------------------------------- splitting
def split(s):
    lx = shlex.shlex(s, posix=True, punctuation_chars="|&;<>")
    lx.whitespace_split = True
    out = []
    for t in lx:
        if out and t in (">", ">>", ">&") and out[-1] in ("1", "2", "&"):
            fd = out.pop()
            t = ("&" if fd == "&" else fd if fd == "2" else "") + t
        if t == ">&" and out:  # 2>&1
            t = ">&"
        out.append(t)
    # 2 >& 1 -> 2>&1
    i = 0
    while i < len(out):
        if out[i] in ("2>&", "2>") and i + 1 < len(out) and out[i + 1] in ("1", "&1"):
            out[i:i + 2] = ["2>&1"]
        elif out[i] == "2>" and i + 1 < len(out) and out[i + 1].startswith("&"):
            out[i:i + 2] = ["2>&1"]
        i += 1
    return out


def segments(toks):
    """[(operator_before or None, [words], [(redirect, target)])]"""
    segs, cur, redir, op = [], [], [], None
    i = 0
    while i < len(toks):
        t = toks[i]
        if cur and cur[0] == "find" and t in (";", "+") and "-exec" in cur + ["-execdir"]:
            cur.append(t)          # find -exec … \; ends the -exec, not the command
        elif t in ("|", "|&", "&&", "||", ";", "&"):
            segs.append((op, cur, redir))
            cur, redir, op = [], [], t
        elif t in (">", ">>", "<", "2>", "2>>", "&>") and i + 1 < len(toks):
            redir.append((t, toks[i + 1]))
            i += 1
        elif t == "2>&1":
            redir.append((t, ""))
        else:
            cur.append(t)
        i += 1
    segs.append((op, cur, redir))
    return [s for s in segs if s[1] or s[2]]


# ---------------------------------------------------------------- docs for each part
def _safe_to_ask_help(cmd):
    if "/" in cmd or cmd in NO_HELP or cmd.startswith("mkfs"):
        return False
    path = shutil.which(cmd)
    return bool(path) and os.path.realpath(path).startswith(SAFE_DIRS)


def help_text(cmd, lib=None):
    """Option docs for `cmd` from this machine: --help, else the man page (library or `man`)."""
    text = ""
    if _safe_to_ask_help(cmd):
        try:
            r = subprocess.run([cmd, "--help"], capture_output=True, text=True, timeout=1.5,
                               stdin=subprocess.DEVNULL,
                               env=dict(os.environ, LC_ALL="C", PAGER="cat", MANPAGER="cat"))
            text = (r.stdout or r.stderr)[:60000]
        except (OSError, subprocess.TimeoutExpired):
            pass
    man = ""
    if lib is not None and getattr(lib, "con", None):
        try:
            rows = lib.con.execute("SELECT body FROM docs WHERE docs MATCH ? AND title = ? "
                                   "AND source = 'man'", (f'title:"{cmd}"', cmd)).fetchall()
            man = "\n\n".join(r[0] for r in rows)
        except Exception:
            man = ""
    if not man and shutil.which("man"):
        try:
            r = subprocess.run(["man", cmd], capture_output=True, text=True, timeout=3,
                               stdin=subprocess.DEVNULL,
                               env=dict(os.environ, LC_ALL="C", MANPAGER="cat", PAGER="cat",
                                        MANWIDTH="200", MAN_KEEP_FORMATTING=""))
            man = re.sub(r".\x08", "", r.stdout or "")[:200000]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return text + "\n\n" + man


def _clean_desc(d):
    d = re.sub(r"\s+", " ", d).strip(" ;:")
    if d.endswith(".") and not d.endswith(" ."):
        d = d[:-1]
    d = re.split(r"(?<=[a-z)])\. (?=[A-Z])", d)[0]       # first sentence
    if len(d) > 80:
        d = d[:80].rsplit(" ", 1)[0].rstrip(",;") + "…"
    return d


def option_docs(text):
    """{'-f': 'ignore nonexistent files…', '--force': …} from --help / man text."""
    docs = {}
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line.startswith("-") or len(line) < 2 or line[1] == " ":
            continue
        parts = re.split(r"\s{2,}|\t", line, maxsplit=1)
        head = parts[0]
        desc = parts[1] if len(parts) > 1 else ""
        if not desc:  # man style: description on the next (more indented) lines
            indent = len(raw) - len(raw.lstrip())
            for nxt in lines[i + 1:i + 4]:
                if not nxt.strip():
                    if desc:
                        break
                    continue
                if len(nxt) - len(nxt.lstrip()) <= indent and nxt.strip().startswith("-"):
                    break
                desc += " " + nxt.strip()
                if desc.rstrip().endswith("."):
                    break
        if not desc:
            continue
        for name in re.findall(r"(?<![\w-])(--?[A-Za-z0-9?][\w-]*)", head):
            docs.setdefault(name, _clean_desc(desc))
    return docs


def subcommand_doc(cmd, sub, text):
    known = SUBCOMMANDS.get(cmd, {})
    if sub in known:
        return known[sub]
    for raw in text.splitlines():  # "   install pkg...      install packages" style lines
        m = re.match(rf"\s{{2,}}{re.escape(sub)}(?:\s+[\[<A-Z][^\s]*)*\s{{2,}}(\S.{{3,}})$", raw)
        if m:
            return _clean_desc(m.group(1))
    return None


def whatis(cmd, summaries=None):
    if summaries and cmd in summaries:
        return summaries[cmd]
    if shutil.which("whatis"):
        try:
            out = subprocess.run(["whatis", cmd], capture_output=True, text=True, timeout=2).stdout
            m = re.search(rf"^{re.escape(cmd)} \(\d\w*\)\s+- (.*)", out, re.M)
            if m:
                return _clean_desc(m.group(1))
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def flag_parts(tok, docs):
    """'-rf' -> ['-r', '-f'] (unless '-rf' is an option itself); '--color=auto' -> ['--color']."""
    if tok.startswith("--"):
        return [tok.split("=", 1)[0]]
    if tok in docs or len(tok) <= 2:
        return [tok]
    head = tok.split("=", 1)[0]
    if head in docs:
        return [head]
    if re.fullmatch(r"-[a-z]{3,}", tok) and "-" + tok[1] not in docs:
        return [tok]                                   # find -name, java -jar…
    out = []
    for ch in tok[1:]:
        f = "-" + ch
        out.append(f)
        if f not in docs and not ch.isalpha():
            break
    return out


# ---------------------------------------------------------------- what could go wrong
def warnings_for(cmd, args, redir, raw):
    w = []
    flags = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    longs = {a.split("=")[0] for a in args if a.startswith("--")}
    targets = [a for a in args if not a.startswith("-")]
    big = [t for t in targets if re.fullmatch(SYSTEM_PATHS, t)]
    if cmd == "rm":
        rec = "r" in flags.lower() or "--recursive" in longs
        if big:
            w.append(f"⚠ DANGER: this would delete `{big[0]}`" + (" and everything under it" if rec else "")
                     + ". Don't run it!")
        else:
            w.append("⚠ Deletes " + ("folders and everything in them" if rec else "files")
                     + " for good: no Trash, no undo." + (" Never asks first." if "f" in flags else ""))
    elif cmd == "dd" and any(a.startswith("of=/dev/") for a in args):
        dev = next(a[3:] for a in args if a.startswith("of=/dev/"))
        w.append(f"⚠ DANGER: overwrites the whole drive `{dev}`. Triple-check it's the right one (`lsblk`).")
    elif cmd.startswith("mkfs") or cmd in ("wipefs", "fdisk", "sfdisk", "cfdisk", "parted", "gdisk", "sgdisk"):
        w.append("⚠ DANGER: changes or erases a disk's partitions/filesystem. Data on it can be lost.")
    elif cmd == "shred":
        w.append("⚠ Destroys the file's contents beyond recovery.")
    elif cmd in ("chmod", "chown", "chgrp"):
        rec = "R" in flags or "--recursive" in longs
        if rec and big:
            w.append(f"⚠ DANGER: changes `{big[0]}` and everything under it. Can break your system.")
        if cmd == "chmod" and any(re.fullmatch(r"0?777|a\+rwx|o\+w", a) for a in args):
            w.append("⚠ 777 lets anyone change or replace these files.")
    elif cmd in ("kill", "killall", "pkill"):
        if "9" in flags or "-KILL" in args or "-SIGKILL" in args:
            w.append("⚠ Force-kills: the program can't save anything first.")
        else:
            w.append("Stops the matching program(s).")
    elif cmd in ("reboot", "shutdown", "poweroff", "halt"):
        w.append("⚠ Restarts / turns off the computer. Save your work first.")
    elif cmd == "find" and ("-delete" in args or any(a in ("rm", "/bin/rm") for a in args)):
        w.append("⚠ Deletes every file it finds, for good. Run it without -delete first to see the list.")
    elif cmd == "git":
        sub = targets[0] if targets else ""
        if sub == "reset" and "--hard" in longs:
            w.append("⚠ Throws away uncommitted changes for good.")
        elif sub == "clean" and "f" in flags:
            w.append("⚠ Deletes untracked files for good. `git clean -n` previews.")
        elif sub == "push" and ("--force" in longs or "f" in flags):
            w.append("⚠ Overwrites the remote branch: others can lose commits. Prefer --force-with-lease.")
        elif sub == "checkout" and "--" in args or sub == "restore":
            w.append("⚠ Discards your uncommitted edits to those files.")
    elif cmd in ("apt", "apt-get") and targets and targets[0] in ("remove", "purge", "autoremove"):
        w.append("⚠ Removes software. Read the list apt shows before saying yes.")
    elif cmd == "systemctl" and targets and targets[0] in ("stop", "disable", "mask"):
        w.append("Stops / disables a service: whatever depends on it stops too.")
    elif cmd == "crontab" and "r" in flags:
        w.append("⚠ Deletes ALL your scheduled cron jobs, no undo.")
    elif cmd in ("userdel", "deluser"):
        w.append("⚠ Removes a user account" + (" and its files" if "r" in flags or "--remove-home" in longs else "") + ".")
    elif cmd in ("truncate",) or cmd == "mv" and big:
        w.append("⚠ Can wipe or move away important files.")
    for op, target in redir:
        if op in (">", "&>") and re.match(r"/dev/(sd|nvme|hd|vd|mmcblk)", target):
            w.append(f"⚠ DANGER: writes straight onto the drive `{target}`, destroying what's on it.")
        elif op in (">", "&>") and target and target != "/dev/null":
            w.append(f"⚠ Replaces everything in `{target}` (use >> to add instead).")
    return w


def global_warnings(raw):
    w = []
    if re.search(r"(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", raw):
        w.append("⚠ Runs a script straight from the internet without you seeing it first. "
                 "Download it, read it, then run it.")
    if re.search(r":\(\)\s*\{.*:\|:.*\};\s*:", raw):
        w.append("⚠ DANGER: a fork bomb. It freezes the computer. Don't run it.")
    if re.search(r"\bsudo\b", raw):
        w.append("⚠ Runs as administrator (root), so it can change anything.")
    return w


# ---------------------------------------------------------------- the breakdown
def breakdown(cmd_line, lib=None, summaries=None, max_lines=9):
    """{'lines': [...], 'warnings': [...], 'commands': [...]} for a command line. Never runs it."""
    raw = cmd_line.strip()
    subs = re.findall(r"\$\(([^()]*)\)", raw)
    flat = re.sub(r"\$\(([^()]*)\)", "SUBST", raw)
    try:
        toks = split(flat)
    except ValueError:
        return {"lines": ["I can't read that: a quote isn't closed."], "warnings": [], "commands": []}
    lines, warns, cmds = [], [], []
    cwd = None                      # where an earlier `cd` in this line went
    for op, words_, redir in segments(toks):
        if op:
            lines.append(f"`{op}` {OPERATORS.get(op, '')}".rstrip())
        # wrappers (sudo, nohup…) and VAR=value prefixes
        while words_ and (words_[0] in WRAPPERS or re.fullmatch(r"\w+=\S*", words_[0])):
            w0 = words_.pop(0)
            if w0 in WRAPPERS:
                lines.append(f"`{w0}` {WRAPPERS[w0]}")
                while w0 == "sudo" and words_ and words_[0].startswith("-"):
                    words_.pop(0)
            else:
                lines.append(f"`{w0}` sets {w0.split('=')[0]} just for this command")
        if words_:
            cmd, args = words_[0], words_[1:]
            name = os.path.basename(cmd)
            cmds.append(name)
            if name == "cd":                          # cd ~/tux: say where, and remember it
                where = args[0] if args else "~"
                lines.append(f"`cd {where}` " + ("goes back to the last folder" if where == "-"
                                                 else f"goes into the folder {where}"))
                cwd = None if where == "-" else os.path.normpath(os.path.join(
                    cwd or os.getcwd(), os.path.expanduser(where)))
                continue
            if "/" in cmd:                            # ./install.sh: a script, not a program
                lines.append(f"`{cmd}` " + script_summary(cmd, cwd))
                warns.append("Runs a script: it can do anything you can. Only run ones you trust.")
                continue
            desc = SHELL_BUILTINS.get(name) or whatis(name, summaries)
            lines.append(f"`{name}` " + (desc or "(no manual page here)"))
            if name == "tar" and args and re.fullmatch(r"[A-Za-z]+", args[0]):
                args = ["-" + args[0]] + args[1:]       # tar xzf -> tar -xzf
            text = help_text(name, lib) if args and name not in SHELL_BUILTINS else ""
            docs = option_docs(text) if text else {}
            docs.update(BUILTIN_FLAGS.get(name, {}))     # plain words beat the man page's
            sub = next((a for a in args if not a.startswith("-")), None)
            if name == "git" and sub and re.fullmatch(r"[a-z-]+", sub):  # git reset -> man git-reset
                docs = {**docs, **option_docs(help_text(f"git-{sub}", lib))}
            positional_seen = False
            for a in args:
                if a == "--" or a == "SUBST":
                    continue
                kv = re.fullmatch(r"(\w+)=(.*)", a)
                if kv and name in ("export", "alias", "set", "declare"):   # export PATH=…
                    lines.append(f"`{kv.group(1)}=…` sets {kv.group(1)} to {kv.group(2) or 'nothing'}")
                elif kv and kv.group(1) in docs:              # dd if=… of=…
                    lines.append(f"`{a}` " + docs[kv.group(1)].replace("{}", kv.group(2)))
                elif a.startswith("-") and len(a) > 1 and (a in docs or not re.fullmatch(r"[-+]\d+", a)):
                    for f in flag_parts(a, docs):
                        d = docs.get(f) or SUBCOMMANDS.get(name, {}).get(f)
                        lines.append(f"`{f}` " + (d or "an option (not in this machine's docs)"))
                elif not positional_seen:
                    positional_seen = True
                    d = subcommand_doc(name, a, text) if re.fullmatch(r"[a-z][\w-]*", a) else None
                    if d:
                        lines.append(f"`{a}` {d}")
            warns += warnings_for(name, args, redir, raw)
        for op2, target in redir:
            what = OPERATORS.get(op2, op2)
            if op2 == "2>&1":
                lines.append(f"`2>&1` {what}")
            elif target == "/dev/null":
                lines.append(f"`{op2} /dev/null` throws " + ("the errors" if op2.startswith("2") else "it") + " away")
            else:
                lines.append(f"`{op2} {target}` {what} `{target}`")
    for s_ in subs:
        lines.append(f"`$({s_})` runs `{s_}` first and puts its output there")
        inner = breakdown(s_, lib, summaries, max_lines=3)
        warns += inner["warnings"]
        cmds += inner["commands"]
    warns = global_warnings(raw) + warns
    rank = lambda w: 0 if "DANGER" in w else 1 if w.startswith("⚠") else 2
    seen, uniq = set(), []
    for w in sorted(warns, key=rank):
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [f"…and {len(lines) - max_lines + 1} more parts"]
    return {"lines": lines, "warnings": uniq, "commands": cmds}


def script_summary(cmd, cwd=None):
    """What a script says it does: its first comment lines, read (never run) from the file.
    `cwd`: the folder an earlier `cd` in the same line went to."""
    base = cwd or os.getcwd()
    path = os.path.normpath(os.path.join(base, os.path.expanduser(cmd)))
    name = os.path.basename(cmd)
    if not cwd and not cmd.startswith(("/", "~")):
        # Tux can't see which folder your terminal is in, so he can't know which file it is
        return f"runs the script {name} from the folder your terminal is in"
    else:
        here = f"in {os.path.dirname(path).replace(os.path.expanduser('~'), '~', 1)}"
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return f"runs the script {name} {here} (I don't run or read scripts I can't find)"
    if b"\0" in head:
        return f"runs the program {name} {here}"
    said = []
    for line in head.decode("utf-8", "ignore").splitlines()[:15]:
        line = line.strip()
        if line.startswith("#!") or not line:
            if said:
                break
            continue
        if not line.startswith("#"):
            break
        said.append(line.lstrip("# ").strip())
        if len(" ".join(said)) > 140:
            break
    gist = re.sub(r"\s+", " ", " ".join(said)).strip()
    if len(gist) > 120:
        gist = gist[:120].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return f"runs the script {name} {here}" + (f". It says: “{gist}”" if gist else "")


def as_text(b):
    """The bubble text: warnings first, then one line per part."""
    out = b["warnings"] + ["• " + l for l in b["lines"]]
    return "\n".join(out)


def load_summaries():
    try:
        with open(os.path.join(HERE, "index.json")) as f:
            return {n: d for n, d in json.load(f)["man"]}
    except (OSError, ValueError, KeyError):
        return {}


def main(argv):
    if not argv:
        print("usage: python3 ~/tux/tux.py --explain 'some command'   (it is never run)")
        return 2
    try:
        from knowledge import Library
        lib = Library()
    except Exception:
        lib = None
    line = " ".join(argv)
    print(as_text(breakdown(line, lib, load_summaries(), max_lines=20)))
    print("(Not run: Tux only reads the docs.)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
