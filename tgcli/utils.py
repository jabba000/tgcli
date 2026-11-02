"""Text helpers (cell-width aware), formatting, clipboard."""
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime

URL_RE = re.compile(r"https?://[^\s<>\"']+")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u2028\u2029]")
_TOK = re.compile(r"\s+|\S+")


def clean(s):
    """Strip control chars that would garble the screen; expand tabs."""
    return _CTRL.sub("", (s or "").replace("\r", "").replace("\t", "    "))


def cw(ch):
    if unicodedata.combining(ch) or ch == "\ufe0f":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def swidth(s):
    return sum(cw(c) for c in s)


def truncate(s, width, ellipsis="…"):
    if width <= 0:
        return ""
    if swidth(s) <= width:
        return s
    out, used = [], 0
    for ch in s:
        c = cw(ch)
        if used + c > width - 1:
            break
        out.append(ch)
        used += c
    return "".join(out) + ellipsis


def pad(s, width):
    s = truncate(s, width)
    return s + " " * (width - swidth(s))


def wrap(text, width):
    """Greedy word wrap by terminal cell width; keeps explicit newlines."""
    width = max(width, 1)
    out = []
    for para in text.split("\n"):
        line, lw = "", 0
        for tok in _TOK.findall(para):
            tw = swidth(tok)
            if lw + tw <= width:
                line += tok
                lw += tw
                continue
            if tok.isspace():
                out.append(line.rstrip())
                line, lw = "", 0
                continue
            if line.strip():
                out.append(line.rstrip())
                line, lw = "", 0
            if tw <= width:
                line, lw = tok, tw
            else:
                line, lw = "", 0
                for ch in tok:
                    c = cw(ch)
                    if lw + c > width:
                        out.append(line)
                        line, lw = "", 0
                    line += ch
                    lw += c
        out.append(line.rstrip())
    return out or [""]


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_duration(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def _local(dt):
    return dt.astimezone() if dt.tzinfo else dt


def fmt_time(dt):
    return _local(dt).strftime("%H:%M") if dt else ""


def fmt_day(dt):
    return _local(dt).strftime("%d %b %Y")


def fmt_chat_time(dt):
    if not dt:
        return ""
    d, now = _local(dt), datetime.now().astimezone()
    if d.date() == now.date():
        return d.strftime("%H:%M")
    if d.year == now.year:
        return d.strftime("%d %b")
    return d.strftime("%b %Y")


def copy_to_clipboard(text):
    """Returns True on success."""
    if sys.platform == "darwin":
        cmds = [["pbcopy"]]
    else:
        cmds = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]]
    for cmd in cmds:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True, timeout=3)
                return True
            except Exception:
                continue
    return False


def cleanup_old_files(directory, days):
    if not days:
        return
    import time
    limit = time.time() - days * 86400
    for root, _, files in os.walk(directory):
        for f in files:
            p = os.path.join(root, f)
            try:
                if os.path.getmtime(p) < limit:
                    os.unlink(p)
            except OSError:
                pass
