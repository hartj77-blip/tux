# Tux

A tiny, fully local Linux helper. Tux is a penguin-shaped desktop window with a
speech bubble. Ask him anything about Linux and he answers in a line or two with
the exact command, right on your machine. No cloud, no account.

![Tux answering a question](tux-art.png)

## Install

```bash
git clone https://github.com/hartj77-blip/tux.git ~/tux
cd ~/tux && ./install.sh
```

That's it. Open **Tux** from your app grid. The installer needs no sudo. It
downloads llama.cpp (~25 MB) and a small model (~1 GB), then takes a few minutes.

**Needs:** x86_64 Linux, `curl`, and Python 3 with GTK4. Stock Ubuntu 22.04+ or Debian 12+ with GNOME
already has everything. Otherwise:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 python3-cairo
sudo apt install python3-pil   # only if you want to use your own Tux picture
```

`install.sh` checks for these and tells you if anything's missing.

## Using Tux

| Do this | What happens |
|---|---|
| Click his belly or just start typing | Ask a question, press Enter |
| Click a command in the bubble | Copied to your clipboard (Tux never runs anything) |
| `Ctrl+C` / right-click the bubble | Copy the first command / the whole answer |
| Copy an error, then click Tux | He offers to explain it: just press Enter |
| Ask "why did that fail?" | He looks at your last few terminal commands |
| Ask "install obs studio", "remove htop", "do I have git?" | Checked against your real apt/snap packages: the exact command that works on your machine, "already installed", or the closest matches |
| Ask for "tips" or "tricks" | Instant power-user moves |
| Click the 📖 link | Opens the matching GNOME Help page |
| Drag him | Move |
| Drag the lines on his right foot / `Ctrl+scroll` | Resize |
| Yellow / red button on his head | Minimize / close |
| Hover over him | Brings his speech bubble back (it tucks away when idle) |
| Right-click him | **Settings** (see below) |

### Two ways to have him around

| | **Free Tux** (default) | **Mini Tux** (lowest profile) |
|---|---|---|
| Size | Resizable | Tiny penguin (44 px) and a compact text box that grows to fit, then shrinks back |
| Moving | Drag anywhere | Drag him and he slides along the screen edges |
| Asking | Click his belly or just type | Click him or just type |
| On top of other windows | Optional (Settings → *Keep on top*) | Always, on every workspace |

### Settings (right-click Tux)

Click a line to change it; it applies right away and is remembered (`~/.config/tux/settings.json`).

| Setting | Choices |
|---|---|
| Mode | Free Tux / Mini Tux |
| Bubble | right / left / above / below Tux (Mini Tux flips it if it would point off-screen) |
| See-through | solid / a little / half / glass (text stays solid) |
| Text size | small / normal / large |
| Theme | light / dark |
| Hide bubble after | 10 s / 20 s / 1 min / never |
| Keep on top | on / off (Mini Tux is always on top) |
| Arch Wiki | off (default) / on: optional offline add-on, downloaded on request; can be deleted again |

Asking him to "stay on top" also offers the visible options. He remembers where you left him.

Nothing extra to install: GNOME on Wayland doesn't let apps keep themselves on top, so Tux runs
through XWayland (Ubuntu's built-in support for X11 apps), where that's allowed. To run him as a
plain Wayland app instead, set `TUX_WAYLAND=1`; he then can't stay on top or ride the edges.

## How it stays fast and light

- **Instant answers:** about 25 common tasks (ports, disk space, wifi, updates…) have
  hand-checked answers that skip the model entirely.
- **Grounded answers:** everything else goes to a local Qwen2.5-Coder 1.5B model via
  llama.cpp, fed the best matches from GNOME Help and man pages. Answers usually take 1–2 s.
- **Light on resources:** the model loads only when you start typing, runs on 3 CPU
  threads at low priority, and unloads after 5 minutes idle. With the model unloaded,
  Tux uses about 70 MB of RAM. Settings are at the top of `tux.py`.
- **Private:** recent commands are only read when your question points at them
  ("that", "it", "why…"). Lines that look like passwords or tokens are skipped.
  Nothing leaves your machine.

## Offline Linux library

`install.sh` builds a local, searchable library in `knowledge/` (SQLite full-text search, no
network needed after setup):

- **Full man pages** for every installed command, admin tool and config file (sections 1, 5, 8)
- **tldr pages**: short real-world examples for ~5,000 commands (3 MB download)
- **Live `--help`** from the commands you actually have installed (destructive tools are never run)
- **The Arch Wiki** (optional add-on, off by default): deep guides on boot, audio, networking,
  systemd and more. Turn it on in Settings → More settings → *Arch Wiki*; Tux downloads it in the
  background (19 MB, ~130 MB on disk). The same setting switches it off or deletes it.

What Tux does with it:

- **Instant tldr answers:** "curl download a file", "chown a folder recursively", "scp copy a
  file to another computer"… When an example clearly matches your question, you get it
  straight away, with no model involved.
- **A source link** under answers when there's a good one: the matching GNOME Help page, or the
  Arch Wiki page if you've turned it on.

Rebuild any time with `python3 knowledge.py build`. Search it yourself with
`python3 knowledge.py search "rsync exclude"`.

## Extras

- **Your own notes:** put `command : description` lines in `~/Helpful Linux Commands.txt`
  and Tux will use them.
- **Your own Tux art:** `python3 setup_tux.py path/to/picture.jpg` cuts out the white
  background and reshapes the window to fit.
- **Recent commands:** bash normally saves history only when a terminal closes. To let
  Tux see what you just ran, add this line to `~/.bashrc`. The installer offers to do it
  for you; it takes effect in new terminals.

  ```bash
  PROMPT_COMMAND="history -a${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
  ```

## Credits & license

Tux the penguin artwork was created by Larry Ewing (lewing@isc.tamu.edu) using The GIMP.

Code is MIT licensed (see `LICENSE`). The model (Qwen2.5-Coder, Apache-2.0) and
llama.cpp (MIT) are downloaded from their own projects by `install.sh`, as are the
[tldr pages](https://github.com/tldr-pages/tldr) (CC BY 4.0, © the tldr-pages authors) and the
[ArchWiki](https://wiki.archlinux.org) (GNU FDL 1.3, © ArchWiki contributors). None of them
are included in this repository.
