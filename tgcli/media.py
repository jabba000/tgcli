"""External programs: mailcap viewers, voice recording, file picker, notifications."""
import fnmatch
import logging
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("tgcli")


# ---------------------------------------------------------------- mailcap
# (stdlib `mailcap` is gone in Python 3.13, so a minimal parser lives here)

def parse_mailcap(path):
    entries = []
    try:
        text = Path(path).expanduser().read_text(errors="replace")
    except OSError:
        return entries
    lines, buf = [], ""
    for raw in text.splitlines():
        if raw.lstrip().startswith("#") and not buf:
            continue
        if raw.endswith("\\"):
            buf += raw[:-1]
            continue
        lines.append(buf + raw)
        buf = ""
    for line in lines:
        if not line.strip():
            continue
        parts, cur, i = [], "", 0
        while i < len(line):
            ch = line[i]
            if ch == "\\" and i + 1 < len(line):
                cur += line[i + 1]
                i += 2
                continue
            if ch == ";":
                parts.append(cur.strip())
                cur = ""
            else:
                cur += ch
            i += 1
        parts.append(cur.strip())
        if len(parts) >= 2 and parts[1]:
            entries.append((parts[0].lower(), parts[1]))
    return entries


def find_viewer(entries, mime, path):
    """Return a shell command for `mime`, exact matches win over wildcards."""
    mime = (mime or "").lower()
    exact = [c for t, c in entries if t == mime]
    wild = [c for t, c in entries if t != mime and fnmatch.fnmatch(mime, t)]
    cmds = exact + wild
    if not cmds:
        return None
    cmd = cmds[0]
    q = shlex.quote(path)
    cmd = cmd.replace('"%s"', q).replace("'%s'", q).replace("%s", q).replace("%t", shlex.quote(mime))
    return cmd


def guess_mime(path):
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def system_opener():
    if sys.platform == "darwin":
        return "open"
    return "xdg-open" if shutil.which("xdg-open") else None


def detached_open(target):
    opener = system_opener()
    if not opener:
        return False
    subprocess.Popen([opener, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    return True


# ---------------------------------------------------------------- voice
class VoiceRecorder:
    """Records with the configured ffmpeg command; ffmpeg is stopped by sending 'q' on stdin
    so the Ogg/Opus file gets finalised properly."""

    def __init__(self, cmd_template, path):
        self.path = str(path)
        self.cmd = shlex.split(cmd_template.format(file_path=shlex.quote(self.path)))
        self.proc = None
        self.started = 0.0

    def start(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.started = time.time()
        time.sleep(0.2)
        if self.proc.poll() is not None:
            raise RuntimeError("recorder exited immediately (check VOICE_RECORD_CMD / microphone)")

    @property
    def elapsed(self):
        return time.time() - self.started

    def stop(self):
        dur = self.elapsed
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except Exception:
                    self.proc.kill()
        return dur

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
        try:
            os.unlink(self.path)
        except OSError:
            pass


# ---------------------------------------------------------------- file picker
def pick_file(cmd_template):
    """Run the external picker (curses must already be suspended). Returns path or None."""
    if not cmd_template or not shutil.which(shlex.split(cmd_template)[0]):
        return None
    if "{file_path}" in cmd_template:
        fd, tmp = tempfile.mkstemp(prefix="tgcli-pick-")
        os.close(fd)
        try:
            subprocess.run(cmd_template.format(file_path=shlex.quote(tmp)), shell=True)
            chosen = Path(tmp).read_text().strip().splitlines()
        finally:
            os.unlink(tmp)
        path = chosen[0] if chosen else ""
    else:  # e.g. "fzf": picked path is printed on stdout
        res = subprocess.run(cmd_template, shell=True, stdout=subprocess.PIPE, text=True)
        path = res.stdout.strip().splitlines()[0] if res.stdout.strip() else ""
    return path if path and os.path.isfile(path) else None


# ---------------------------------------------------------------- notifications
def notify(cfg, title, subtitle, msg):
    if not cfg.NOTIFY or not cfg.NOTIFY_CMD:
        return
    try:
        cmd = cfg.NOTIFY_CMD.format(
            title=shlex.quote(title or ""), subtitle=shlex.quote(subtitle or ""),
            msg=shlex.quote(msg or ""), icon_path=shlex.quote(""))
        subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        log.exception("notification failed")
