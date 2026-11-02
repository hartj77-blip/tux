#!/usr/bin/env python3
"""One-time prep for Tux (re-run any time):
  1. index GNOME Help pages + man page summaries -> index.json
  2. optional: use your own Tux picture (white background gets cut out)
     -> tux-art.png + tux-art.json
Usage: python3 setup_tux.py [path/to/your-tux-image]
"""
import glob
import html
import json
import os
import re
import subprocess
import sys


HERE = os.path.dirname(os.path.abspath(__file__))


def make_art(src):
    from PIL import Image, ImageDraw, ImageFilter  # only needed for custom art
    im = Image.open(src).convert("RGB")
    w, h = im.size
    # flood-fill the white background from every border pixel with a marker colour
    marker = (255, 0, 255)
    work = im.copy()
    for x in range(0, w, 4):
        for y in (0, h - 1):
            if work.getpixel((x, y)) != marker and min(work.getpixel((x, y))) > 200:
                ImageDraw.floodfill(work, (x, y), marker, thresh=45)
    for y in range(0, h, 4):
        for x in (0, w - 1):
            if work.getpixel((x, y)) != marker and min(work.getpixel((x, y))) > 200:
                ImageDraw.floodfill(work, (x, y), marker, thresh=45)
    mask = Image.new("L", (w, h), 255)
    mp, wp = mask.load(), work.load()
    for y in range(h):
        for x in range(w):
            if wp[x, y] == marker:
                mp[x, y] = 0
    # soften the cut edge so there's no jagged white halo
    mask = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.8))
    out = im.convert("RGBA")
    out.putalpha(mask)
    out.save(os.path.join(HERE, "tux-art.png"))
    # clickable outline: horizontal runs of solid pixels, every 2nd row
    runs, mp = [], mask.load()
    for y in range(0, h, 2):
        x = 0
        while x < w:
            if mp[x, y] > 100:
                x0 = x
                while x < w and mp[x, y] > 100:
                    x += 1
                runs.append([y, x0, x])
            x += 1
    with open(os.path.join(HERE, "tux-art.json"), "w") as f:
        json.dump({"w": w, "h": h, "runs": runs}, f)
    print(f"art: {w}x{h}, {len(runs)} outline runs")


def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def make_index():
    pages = []
    for path in glob.glob("/usr/share/help/C/*/*.page"):
        book = path.split("/")[-2]
        try:
            txt = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        pid = re.search(r'<page[^>]*\bid="([^"]+)"', txt)
        title = re.search(r"<title[^>]*>(.*?)</title>", txt, re.S)
        if not pid or not title:
            continue
        desc = re.search(r"<desc[^>]*>(.*?)</desc>", txt, re.S)
        body = txt.split("</info>", 1)[-1]
        body = re.sub(r"<title.*?</title>", " ", body, flags=re.S)
        paras = [strip_tags(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S)]
        steps = [strip_tags(p) for p in re.findall(r"<item>\s*<p>(.*?)</p>", body, re.S)]
        pages.append({
            "uri": f"help:{book}/{pid.group(1)}",
            "title": strip_tags(title.group(1)),
            "desc": strip_tags(desc.group(1)) if desc else "",
            "text": " ".join(paras[:2])[:260],
            "steps": steps[:4],
            "words": " ".join(paras)[:1500],
        })
    mans = []
    out = subprocess.run(["man", "-k", "."], capture_output=True, text=True).stdout
    for line in out.splitlines():
        m = re.match(r"(\S+) \((\d)[^)]*\)\s+- (.*)", line)
        if m and m.group(2) in "18":
            mans.append([m.group(1), m.group(3)[:110]])
    with open(os.path.join(HERE, "index.json"), "w") as f:
        json.dump({"help": pages, "man": mans}, f)
    print(f"index: {len(pages)} help pages, {len(mans)} man pages")


if __name__ == "__main__":
    make_index()
    if len(sys.argv) > 1:
        make_art(sys.argv[1])
    elif not os.path.exists(os.path.join(HERE, "tux-art.png")):
        sys.exit("No tux-art.png: run  python3 setup_tux.py path/to/tux-image")
