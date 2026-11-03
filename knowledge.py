#!/usr/bin/env python3
"""Tux's offline Linux library: full man pages, tldr examples and (optionally) the Arch Wiki,
in one SQLite full-text index (knowledge/knowledge.db). Everything stays on this machine.

Build:  python3 knowledge.py build [--arch]     (downloads tldr, and the Arch Wiki with --arch)
Wiki:   python3 knowledge.py add-arch / remove-arch   (the optional offline Arch Wiki)
Search: python3 knowledge.py search "rsync exclude folder"
"""
import gzip
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import time
import urllib.request
import zipfile
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
KDIR = os.path.join(HERE, "knowledge")
SRC = os.path.join(KDIR, "src")             # downloads (kept so a rebuild needs no network)
ARCH_DIR = os.path.join(KDIR, "arch")       # English Arch Wiki pages (opened from Tux's links)
DB = os.path.join(KDIR, "knowledge.db")

TLDR_URL = "https://github.com/tldr-pages/tldr/releases/latest/download/tldr-pages.en.zip"
ARCH_JSON = "https://archlinux.org/packages/extra/any/arch-wiki-docs/json/"
ARCH_MIRROR = "https://geo.mirror.pkgbuild.com/extra/os/x86_64/"
MAN_DIRS = ["/usr/share/man/man1", "/usr/share/man/man8", "/usr/share/man/man5"]
CHUNK = 1100   # characters per indexed piece
SKIP_MAN_SECTIONS = {"SEE ALSO", "AUTHOR", "AUTHORS", "COPYRIGHT", "REPORTING BUGS", "BUGS",
                     "HISTORY", "COLOPHON", "LICENSE", "AVAILABILITY", "STANDARDS", "CONFORMING TO"}


# ---------------------------------------------------------------- downloads
def _download(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "tux-linux-helper"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest + ".part", "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(dest + ".part", dest)


def fetch_tldr():
    dest = os.path.join(SRC, "tldr-pages.en.zip")
    print("  downloading tldr pages (~3 MB)…", flush=True)
    _download(TLDR_URL, dest)
    return dest


def fetch_arch():
    with urllib.request.urlopen(ARCH_JSON, timeout=30) as r:
        meta = json.load(r)
    dest = os.path.join(SRC, "arch-wiki-docs.pkg.tar.zst")
    print(f"  downloading the Arch Wiki ({meta['compressed_size'] // 1048576} MB)…", flush=True)
    _download(ARCH_MIRROR + meta["filename"], dest)
    return dest


def unpack_arch(pkg):
    """Keep only the English pages + shared assets, unpacked safely into knowledge/arch."""
    prefix = "usr/share/doc/arch-wiki/html/"
    tmp = ARCH_DIR + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with tarfile.open(pkg, "r:zst") as tf:
        keep = []
        for m in tf.getmembers():
            if not m.name.startswith(prefix) or not (m.isfile() or m.isdir()):
                continue
            rel = m.name[len(prefix):]
            first = rel.split("/", 1)[0]
            if "/" in rel and first != "en":
                continue  # other languages
            m.name = rel
            if m.name:
                keep.append(m)
        tf.extractall(tmp, members=keep, filter="data")  # 'data' blocks path tricks
    shutil.rmtree(ARCH_DIR, ignore_errors=True)
    os.replace(tmp, ARCH_DIR)


# ---------------------------------------------------------------- man pages -> text
_ESCAPES = [
    (r"\\f(\[[^\]]*\]|\(..|.)", ""), (r"\\s[+-]?\d+", ""), (r"\\\*(\(..|\[[^\]]*\]|.)", ""),
    (r"\\\((aq|cq)", "'"), (r"\\\((dq|lq|rq)", '"'), (r"\\\((em|en|hy)", "-"), (r"\\\(bu", "•"),
    (r"\\\(co", "©"), (r"\\\(rs", "\\\\"), (r"\\\(..", ""), (r"\\\[[^\]]*\]", ""),
    (r"\\-", "-"), (r"\\[&|^%~:]", ""), (r"\\e", "\\\\"), (r"\\ ", " "), (r"\\c", ""),
    (r"\\'", "'"), (r"\\`", "`"), (r"\\\.", "."),
]


def _unescape(s):
    for pat, rep in _ESCAPES:
        s = re.sub(pat, rep, s)
    return s


def _args(rest):
    """Macro arguments, honouring "quoted args"."""
    return [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', rest)]


def roff_to_sections(src):
    """[(section_name, text)] from man(7)/mdoc(7) source; good enough for searching."""
    out, sec, buf = [], "DESCRIPTION", []

    def flush():
        txt = re.sub(r"[ \t]+", " ", "\n".join(buf))
        txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
        if txt:
            out.append((sec, txt))

    for line in src.splitlines():
        if line.startswith(('.\\"', "'\\\"", '.\\#')) or line in (".", "'"):
            continue
        if line.startswith((".", "'")):
            m = re.match(r"[.'](\S+)\s*(.*)", line)
            if not m:
                continue
            mac, rest = m.group(1), m.group(2)
            if mac in ("SH", "Sh", "SS", "Ss"):
                if mac in ("SH", "Sh"):
                    flush()
                    buf = []
                    sec = _unescape(" ".join(_args(rest))).upper() or sec
                else:
                    buf.append("\n" + _unescape(" ".join(_args(rest))) + ":")
                continue
            if mac in ("TH", "Dd", "Dt", "Os", "so", "de", "ds", "nr", "ie", "if", "el", "tr",
                       "Bl", "El", "Bd", "Ed", "ad", "na", "hy", "nh", "ne", "ft", "ps", "in",
                       "ti", "ll", "ta", "rm", "cc", "UE", "ME", "ce", "fam"):
                continue
            if mac in ("PP", "LP", "P", "Pp", "sp", "br", "TP", "TQ", "RS", "RE", "nf", "fi",
                       "EX", "EE", "Lp"):
                buf.append("")
                continue
            if mac in ("B", "I", "SM", "SB", "UR", "MT"):
                buf.append(_unescape(" ".join(_args(rest))))
                continue
            if mac in ("BR", "BI", "IB", "IR", "RB", "RI"):
                buf.append(_unescape("".join(_args(rest))))
                continue
            if mac == "IP":
                a = _args(rest)
                buf.append("\n" + (_unescape(a[0]) if a else ""))
                continue
            # mdoc: .Fl x -> -x, .Ar/.Nm/.Cm -> words, .Op -> [ ], .It -> new line
            toks, words_ = _args(rest), []
            for t in toks:
                if t in ("Fl",):
                    words_.append("-")
                elif t in ("Ar", "Nm", "Cm", "Pa", "Ev", "Li", "Ic", "Em", "Sy", "Xr", "Op",
                           "Oo", "Oc", "Ns", "Ql", "Dq", "Sq", "Ek", "Bk", "Va", "Fn", "Fa"):
                    continue
                else:
                    words_.append(t)
            text = " ".join(words_).replace("- ", "-")
            if mac == "Fl":
                text = "-" + text
            if mac in ("It", "Nd", "Dl", "D1"):
                text = "\n" + ("- " if mac == "Nd" else "") + text
            buf.append(_unescape(text))
            continue
        buf.append(_unescape(line))
    flush()
    return out


def _chunks(text, size=CHUNK):
    """Split at blank lines into pieces of about `size` characters."""
    parts, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if cur and len(cur) + len(para) > size:
            parts.append(cur)
            cur = ""
        while len(para) > size * 1.5:   # one huge paragraph
            parts.append(para[:size])
            para = para[size:]
        cur = (cur + "\n\n" + para).strip()
    if cur:
        parts.append(cur)
    return parts


def man_docs():
    seen = set()
    for d in MAN_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            path = os.path.join(d, fn)
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            m = re.match(r"(.+)\.(\d\w*)(\.gz)?$", fn)
            if not m:
                continue
            name, sect = m.group(1), m.group(2)
            try:
                raw = gzip.open(path, "rt", errors="ignore").read() if fn.endswith(".gz") \
                    else open(path, errors="ignore").read()
            except (OSError, EOFError):
                continue
            if raw.lstrip().startswith(".so "):  # just points at another page
                continue
            for sec, txt in roff_to_sections(raw):
                if sec in SKIP_MAN_SECTIONS:
                    continue
                for piece in _chunks(txt):
                    yield ("man", name, sec.title(), piece, f"man:{name}({sect})")


# ---------------------------------------------------------------- tldr pages -> text
def tldr_docs(zpath):
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            parts = n.split("/")
            if len(parts) != 2 or parts[0] not in ("common", "linux") or not n.endswith(".md"):
                continue
            md = z.read(n).decode("utf-8", "ignore")
            name = parts[1][:-3]
            desc, lines, pending = [], [], None
            for line in md.splitlines():
                line = line.strip()
                if line.startswith(">"):
                    t = re.sub(r"More information: <.*?>\.?", "", line[1:]).strip()
                    if t:
                        desc.append(t)
                elif line.startswith("- "):
                    pending = line[2:].rstrip(":")
                elif line.startswith("`") and pending:
                    cmd = re.sub(r"\{\{(.*?)\}\}", r"\1", line.strip("`"))
                    lines.append(f"- {pending}: `{cmd}`")
                    pending = None
            body = " ".join(desc) + "\n" + "\n".join(lines)
            yield ("tldr", name, "Examples", body.strip(), f"tldr:{name}")


# ---------------------------------------------------------------- Arch Wiki -> text
class _WikiText(HTMLParser):
    """Collects the article text, split at h2/h3 headings (with their #anchors)."""
    SKIP_CLASSES = ("archwiki-template-meta-related-articles", "toc", "navbox", "noprint",
                    "mw-editsection", "catlinks", "printfooter")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0          # >0 while inside the article body
        self.skip = 0           # >0 while inside something to leave out
        self.sections = [["", "", []]]   # heading, anchor, text parts
        self.in_heading = None
        self.title = ""
        self.in_title = False
        self.stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "title":
            self.in_title = True
        if self.depth == 0:
            if tag == "div" and "mw-parser-output" in cls:
                self.depth = 1
                self.stack = ["div"]
            return
        if tag in ("br", "img", "hr", "input", "meta", "link", "wbr"):
            if tag == "br":
                self._add("\n")
            return
        self.stack.append(tag)
        if self.skip or tag in ("style", "script") or any(c in cls for c in self.SKIP_CLASSES):
            self.skip += 1
            return
        if tag in ("h2", "h3", "h4"):
            self.in_heading = [tag, a.get("id", ""), []]
        elif tag in ("p", "li", "pre", "dd", "dt", "tr", "div", "table"):
            self._add("\n")
            if tag == "li":
                self._add("- ")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if self.depth == 0 or not self.stack:
            return
        # pop to the matching tag (HTML is forgiving)
        while self.stack:
            t = self.stack.pop()
            if self.skip:
                self.skip -= 1
            if t == tag:
                break
        if not self.stack:
            self.depth = 0
            return
        if self.in_heading and tag == self.in_heading[0]:
            text = "".join(self.in_heading[2]).strip()
            self.sections.append([text, self.in_heading[1], []])
            self.in_heading = None
        elif tag in ("p", "li", "pre", "dd", "tr"):
            self._add("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.depth == 0 or self.skip:
            return
        if self.in_heading:
            self.in_heading[2].append(data)
        else:
            self._add(data)

    def _add(self, s):
        self.sections[-1][2].append(s)


def arch_docs():
    en = os.path.join(ARCH_DIR, "en")
    if not os.path.isdir(en):
        return
    for fn in sorted(os.listdir(en)):
        if not fn.endswith(".html"):
            continue
        path = os.path.join(en, fn)
        p = _WikiText()
        try:
            p.feed(open(path, encoding="utf-8", errors="ignore").read())
        except Exception:
            continue
        title = re.sub(r"\s*-\s*ArchWiki\s*$", "", p.title).strip()
        # redirect stubs / tiny pages aren't worth indexing
        total = sum(len("".join(parts)) for _, _, parts in p.sections)
        if not title or total < 200:
            continue
        for heading, anchor, parts in p.sections:
            text = re.sub(r"[ \t]+", " ", "".join(parts))
            text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
            if len(text) < 60:
                continue
            ref = "file://" + path + (f"#{anchor}" if anchor else "")
            for piece in _chunks(text):
                yield ("arch", title, heading or "Overview", piece, ref)


# ---------------------------------------------------------------- build
def build(with_arch=False, fetch=True):
    os.makedirs(SRC, exist_ok=True)
    tldr_zip = os.path.join(SRC, "tldr-pages.en.zip")
    if fetch:
        try:
            tldr_zip = fetch_tldr()
        except OSError as e:
            print(f"  (couldn't download tldr pages: {e}; continuing without)")
    if with_arch and (fetch or not os.path.isdir(ARCH_DIR)):
        pkg = os.path.join(SRC, "arch-wiki-docs.pkg.tar.zst")
        try:
            if fetch or not os.path.exists(pkg):
                pkg = fetch_arch()
            print("  unpacking the English Arch Wiki…", flush=True)
            unpack_arch(pkg)
            os.remove(pkg)  # the unpacked pages are all a rebuild needs
        except (OSError, ValueError, tarfile.TarError) as e:
            print(f"  (couldn't get the Arch Wiki: {e}; continuing without)")
    tmp = DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("CREATE VIRTUAL TABLE docs USING fts5(title, heading, body, source UNINDEXED, "
                "ref UNINDEXED, tokenize='porter unicode61 remove_diacritics 2')")
    counts = {}
    sources = [("man pages", man_docs())]
    if os.path.exists(tldr_zip):
        sources.append(("tldr pages", tldr_docs(tldr_zip)))
    if os.path.isdir(ARCH_DIR):
        sources.append(("Arch Wiki", arch_docs()))
    for label, gen in sources:
        t0, n = time.monotonic(), 0
        batch = []
        for row in gen:
            batch.append(row)
            if len(batch) >= 2000:
                con.executemany("INSERT INTO docs(source, title, heading, body, ref) VALUES (?,?,?,?,?)", batch)
                n += len(batch)
                batch = []
        if batch:
            con.executemany("INSERT INTO docs(source, title, heading, body, ref) VALUES (?,?,?,?,?)", batch)
            n += len(batch)
        counts[label] = n
        print(f"  {label}: {n} pieces ({time.monotonic() - t0:.0f} s)", flush=True)
    con.execute("INSERT INTO docs(docs) VALUES('optimize')")
    con.commit()
    con.close()
    os.replace(tmp, DB)
    print(f"  library: {os.path.getsize(DB) // 1048576} MB at {DB}")
    return counts


# ---------------------------------------------------------------- search (used by tux.py)
def have_arch():
    """Is the optional offline Arch Wiki downloaded?"""
    return os.path.isdir(os.path.join(ARCH_DIR, "en"))


class Library:
    def __init__(self, path=DB, use_arch=False):
        self.con = None
        self.use_arch = use_arch and have_arch()   # optional add-on, off unless you turn it on
        if os.path.exists(path):
            self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)

    def __bool__(self):
        return self.con is not None

    @staticmethod
    def _match(terms):
        return " OR ".join('"' + t.replace('"', "") + '"' for t in terms if t)

    def search(self, terms, command=None, limit=4):
        """Best pieces for these words; pieces about `command` first."""
        if not self.con or not terms:
            return []
        q = self._match(terms)
        rows, seen = [], set()
        cols = "source, title, heading, body, ref, bm25(docs, 6.0, 3.0, 1.0) AS score"
        skip = "" if self.use_arch else " AND source != 'arch'"   # Arch Wiki is opt-in
        sql = f"SELECT {cols} FROM docs WHERE docs MATCH ?{skip} ORDER BY score LIMIT ?"
        # exact command name ("tar", not "git-get-tar-commit-id")
        sql_cmd = f"SELECT {cols} FROM docs WHERE docs MATCH ? AND title = ?{skip} ORDER BY score LIMIT ?"
        try:
            if command:
                # its tldr examples first, then the man page parts that match the question
                rows += self.con.execute(
                    f"SELECT {cols} FROM docs WHERE docs MATCH ? AND title = ? AND source = 'tldr' LIMIT 1",
                    (f'title:"{command}"', command)).fetchall()
                if q:
                    rows += self.con.execute(sql_cmd, (f'title:"{command}" AND ({q})', command, 2)).fetchall()
            rows += self.con.execute(sql, (q, limit * 3)).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for src, title, heading, body, ref, score in rows:
            key = (title, heading, body[:40])
            if key in seen:
                continue
            seen.add(key)
            out.append({"source": src, "title": title, "heading": heading, "body": body,
                        "ref": ref, "score": score})
            if len(out) >= limit:
                break
        return out

    def man_mentions_help(self, command):
        if not self.con:
            return False
        try:
            row = self.con.execute("SELECT 1 FROM docs WHERE docs MATCH ? LIMIT 1",
                                   (f'title:"{command}" AND body:"help"',)).fetchone()
        except sqlite3.OperationalError:
            return False
        return bool(row)


def best_window(text, terms, size=360):
    """The `size`-character stretch of `text` that mentions the most search words."""
    text = re.sub(r"\s+", " ", text)
    if len(text) <= size:
        return text
    low = text.lower()
    stems = [t.lower()[:5] for t in terms if len(t) > 2]
    best, best_i = -1, 0
    for i in range(0, len(text) - size + 1, 40):
        win = low[i:i + size]
        score = sum(win.count(s) for s in stems)
        if score > best:
            best, best_i = score, i
    start = text.rfind(" ", 0, best_i) + 1 if best_i else 0
    return ("…" if start else "") + text[start:start + size].rsplit(" ", 1)[0] + "…"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build(with_arch="--arch" in sys.argv, fetch="--offline" not in sys.argv)
    elif len(sys.argv) > 1 and sys.argv[1] == "add-arch":      # optional add-on: get it
        build(with_arch=True, fetch=False)
        sys.exit(0 if have_arch() else 1)
    elif len(sys.argv) > 1 and sys.argv[1] == "remove-arch":   # ...or delete it (~130 MB)
        shutil.rmtree(ARCH_DIR, ignore_errors=True)
        build(with_arch=False, fetch=False)
    elif len(sys.argv) > 2 and sys.argv[1] == "search":
        words_ = sys.argv[2].split()
        cmd = sys.argv[3] if len(sys.argv) > 3 else None
        for r in Library(use_arch=True).search(words_, cmd):
            print(f"[{r['source']}] {r['title']} › {r['heading']}  ({r['score']:.1f})  {r['ref']}")
            print("   ", best_window(r["body"], words_)[:300])
    else:
        print(__doc__)
