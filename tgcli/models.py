"""Plain data classes shared between the backend and the UI."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .utils import human_duration, human_size


@dataclass
class MediaInfo:
    kind: str                 # photo video voice video_note audio sticker gif document poll geo contact unsupported
    name: str = ""
    size: int = 0
    mime: str = ""
    duration: int = 0
    extra: str = ""           # sticker emoji, poll question, "lat,lon", contact name...
    downloadable: bool = True
    thumb_only: bool = False  # animated sticker: only the preview is downloaded

    def label(self):
        parts = [f"[{self.kind}]"]
        if self.extra:
            parts.append(self.extra)
        if self.name:
            parts.append(self.name)
        if self.duration:
            parts.append(human_duration(self.duration))
        if self.size and self.kind not in ("sticker", "photo"):
            parts.append(f"({human_size(self.size)})")
        return " ".join(parts)


@dataclass
class MsgItem:
    id: int
    chat_id: int
    date: datetime
    sender_id: Optional[int]
    sender: str
    outgoing: bool
    text: str = ""
    reply_to_id: Optional[int] = None
    reply_preview: str = ""
    fwd_from: Optional[str] = None
    edited: bool = False
    media: Optional[MediaInfo] = None
    path: Optional[str] = None          # local file once downloaded
    cache_path: Optional[str] = None    # where it would be downloaded to
    state: str = "ok"                   # ok | pending | failed
    downloading: Optional[int] = None   # percent while downloading
    raw: Any = None                     # telethon Message


@dataclass
class ChatItem:
    id: int
    name: str
    entity: Any
    kind: str                           # user bot group channel
    pinned: bool = False
    muted: bool = False
    unread_count: int = 0
    unread_mark: bool = False
    last_text: str = ""
    last_date: Optional[datetime] = None
    last_outgoing: bool = False
    last_msg_id: int = 0
    read_inbox_max_id: int = 0
    read_outbox_max_id: int = 0
    new_after: int = 0                  # msgs newer than this are "new" while the chat is open
    online: bool = False
    status: str = ""
    user_id: Optional[int] = None


def preview(item, limit=60):
    """One-line summary of a message."""
    if item.media:
        s = item.media.label()
        if item.text:
            s += " " + item.text
    else:
        s = item.text
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"
