"""Configuration: defaults + user overrides from ~/.config/tgcli/conf.py (a plain Python file)."""
import os
import runpy
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

CONFIG_DIR = Path(os.environ.get("TGCLI_CONFIG_DIR", Path.home() / ".config" / "tgcli"))
DATA_DIR = Path(os.environ.get("TGCLI_DATA_DIR", Path.home() / ".local" / "share" / "tgcli"))


def _default_notify_cmd():
    if sys.platform == "darwin":
        if shutil.which("terminal-notifier"):
            return ("terminal-notifier -title {title} -subtitle {subtitle} "
                    "-message {msg} -sound default")
        return ("osascript -e 'on run {{m, t}}' -e 'display notification m with title t' "
                "-e 'end run' {msg} {title}")
    if shutil.which("notify-send"):
        return "notify-send -a tgcli {title} {msg}"
    return None


def _default_voice_cmd():
    if not shutil.which("ffmpeg"):
        return None
    if sys.platform == "darwin":
        src = "-f avfoundation -i ':0'"
    else:
        src = "-f pulse -i default"
    return f"ffmpeg -y -loglevel quiet {src} -ac 1 -c:a libopus -b:a 32k {{file_path}}"


def _default_picker():
    for cmd in ("ranger --choosefile={file_path}", "nnn -p {file_path}"):
        if shutil.which(cmd.split()[0]):
            return cmd
    return None


def defaults():
    return dict(
        # --- credentials (get them at https://my.telegram.org -> API development tools)
        API_ID=None,
        API_HASH=None,
        PHONE=None,                      # "+421..." ; asked interactively when None
        # --- storage
        SESSION_PATH=str(DATA_DIR / "session"),
        FILES_DIR=str(DATA_DIR / "files"),
        DOWNLOAD_DIR=str(Path.home() / "Downloads"),
        KEEP_MEDIA=7,                    # days; None = never clean cache
        LOG_LEVEL="INFO",
        LOG_PATH=str(DATA_DIR),
        # --- behaviour
        MAX_CHATS=200,
        MSGS_LOAD=50,
        AUTO_READ=True,                  # mark chat read when you open it / get msgs in it
        AUTO_DOWNLOAD=("photo", "voice", "video_note", "sticker"),
        AUTO_DOWNLOAD_MAX_SIZE=5 * 1024 * 1024,
        # --- external programs
        NOTIFY=True,
        NOTIFY_CMD=_default_notify_cmd(),   # python format string: {title} {subtitle} {msg} {icon_path}
        VOICE_RECORD_CMD=_default_voice_cmd(),
        FILE_PICKER_CMD=_default_picker(),  # {file_path}: temp file that receives the chosen path
        MAILCAP_FILE=str(Path.home() / ".config" / "mailcap"),
        EDITOR=os.environ.get("EDITOR", "vi"),
        # --- look
        CHATS_WIDTH=0.25,                # fraction of the screen
        USERS_COLORS=tuple(range(2, 16)),
        CHAT_FLAGS={
            "online": "●", "pinned": "P", "muted": "M", "unread": "U",
            "unseen": "✓", "seen": "✓✓", "secret": "🔒",
        },
        MSG_FLAGS={
            "selected": "*", "forwarded": "F", "new": "N", "unseen": "U",
            "edited": "E", "pending": "...", "failed": "✗", "seen": "✓✓",
        },
    )


def load_config():
    cfg = defaults()
    path = CONFIG_DIR / "conf.py"
    if path.exists():
        user = runpy.run_path(str(path))
        cfg.update({k: v for k, v in user.items() if k.isupper()})
    c = SimpleNamespace(**cfg)
    for d in (DATA_DIR, Path(c.FILES_DIR)):
        d.mkdir(parents=True, exist_ok=True)
    return c
