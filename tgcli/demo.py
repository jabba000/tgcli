"""`tgcli --demo`: same interface as TelegramBackend, but with fake in-memory data.
Handy for trying the UI or hacking on it without logging in."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from .models import ChatItem, MediaInfo, MsgItem
from .runtime import AsyncRunner

NOW = datetime.now(timezone.utc)


class DemoBackend(AsyncRunner):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.me = SimpleNamespace(id=1)
        self.me_name = "You"
        self._next = 1000
        self.chats = {}
        self.msgs = {}
        self._seed()

    # --------------------------------------------------------------- data
    def _chat(self, cid, name, kind="user", **kw):
        c = ChatItem(id=cid, name=name, entity=None, kind=kind, **kw)
        if kind == "user":
            c.user_id = cid
        self.chats[cid] = c
        self.msgs[cid] = []
        return c

    def _msg(self, cid, sender, text, mins_ago, out=False, media=None, reply_to=None, fwd=None):
        self._next += 1
        sid = 1 if out else abs(hash(sender)) % 997 + 2
        m = MsgItem(id=self._next, chat_id=cid, date=NOW - timedelta(minutes=mins_ago), sender_id=sid,
                    sender=self.me_name if out else sender, outgoing=out, text=text, media=media,
                    reply_to_id=reply_to, fwd_from=fwd)
        if media and media.downloadable:
            m.cache_path = os.path.join(self.cfg.FILES_DIR, "demo", f"{m.id}.png")
        lst = self.msgs[cid]
        if reply_to:
            src = next((x for x in lst if x.id == reply_to), None)
            m.reply_preview = f"{src.sender}: {src.text[:40]}" if src else "(deleted message)"
        lst.append(m)
        c = self.chats[cid]
        c.last_msg_id, c.last_date, c.last_text, c.last_outgoing = m.id, m.date, text, out
        return m

    def _seed(self):
        a = self._chat(10, "Alice", online=True, status="online")
        self._msg(10, "Alice", "Hey! Did you see the new release?", 190)
        m = self._msg(10, "Alice", "Check https://github.com/paul-nameless/tg and https://telethon.dev", 185)
        self._msg(10, "You", "Yes, we're building a CLI client in the same spirit 🚀", 180, out=True)
        self._msg(10, "Alice", "Nice, send me a screenshot when it works", 170, reply_to=m.id)
        self._msg(10, "Alice", "", 120, media=MediaInfo("photo", size=240_000, mime="image/png"))
        self._msg(10, "Alice", "", 60, media=MediaInfo("voice", duration=14, size=40_000, mime="audio/ogg"))
        self._msg(10, "Alice", "This is a deliberately long message to check word wrapping across the "
                  "message pane, including a_very_long_unbreakable_token_" + "x" * 60 + " and some CJK: "
                  "你好，世界！これはテストです。", 30)
        a.unread_count, a.read_inbox_max_id = 2, self._next - 2
        a.read_outbox_max_id = 0

        self._chat(11, "Bob", status="last seen recently")
        self._msg(11, "Bob", "Lunch?", 400)
        self._msg(11, "You", "Sure, 12:30", 395, out=True)

        self._chat(12, "Dev Team", "group", status="group")
        self._msg(12, "Carol", "Build is green ✅", 700)
        self._msg(12, "Dave", "", 650, media=MediaInfo("document", "report-q3.pdf", 1_300_000,
                                                      "application/pdf"))
        self._msg(12, "Carol", "Forwarded from the ops channel", 640, fwd="Ops Alerts")
        self.chats[12].unread_count = 5
        self.chats[12].read_inbox_max_id = 0

        n = self._chat(13, "Tech News", "channel", muted=True)
        self._msg(13, "Tech News", "Python 3.13 removed the `mailcap` module.", 2000)
        s = self._chat(14, "Saved Messages", pinned=True)
        self._msg(14, "You", "remember to buy milk", 3000, out=True)
        self._chat(15, "Mom", pinned=True, status="last seen yesterday")
        self._msg(15, "Mom", "Call me when you can ❤️", 5000)
        self.chats[15].unread_mark = True

    # --------------------------------------------------------------- background simulation
    async def simulate(self):
        await asyncio.sleep(12)
        m = self._msg(10, "Alice", "(demo) a new message just arrived!", 0)
        self.events.put(("new_message", m))
        await asyncio.sleep(6)
        self.events.put(("read", 10, self._next, True))

    # --------------------------------------------------------------- interface
    async def get_chats(self, limit=200):
        if not getattr(self, "_sim", None):
            self._sim = asyncio.ensure_future(self.simulate())
        return list(self.chats.values())

    async def get_contacts(self):
        return [self.chats[i] for i in (10, 11, 15)]

    async def get_messages(self, chat, limit=50, max_id=0):
        items = [m for m in self.msgs[chat.id] if not max_id or m.id < max_id]
        return items[-limit:]

    async def send_text(self, chat, text, reply_to=None):
        await asyncio.sleep(0.4)
        return self._msg(chat.id, "You", text, 0, out=True, reply_to=reply_to)

    async def send_file(self, chat, path, kind="document", reply_to=None):
        for i in range(1, 6):
            self.progress("↑ " + os.path.basename(path), i, 5)
            await asyncio.sleep(0.2)
        return self._msg(chat.id, "You", "", 0, out=True,
                         media=MediaInfo(kind, os.path.basename(path), os.path.getsize(path),
                                         downloadable=False))

    async def send_voice(self, chat, path, duration, reply_to=None):
        await asyncio.sleep(0.5)
        return self._msg(chat.id, "You", "", 0, out=True,
                         media=MediaInfo("voice", duration=int(duration), mime="audio/ogg", downloadable=False))

    async def edit(self, chat, msg_id, text):
        m = next(x for x in self.msgs[chat.id] if x.id == msg_id)
        m.text, m.edited = text, True
        return m

    async def delete(self, chat, ids):
        self.msgs[chat.id] = [m for m in self.msgs[chat.id] if m.id not in ids]

    async def forward(self, dst, src, ids):
        for i in ids:
            m = next(x for x in self.msgs[src.id] if x.id == i)
            self.events.put(("new_message", self._msg(dst.id, "You", m.text, 0, out=True, fwd=m.sender)))

    async def download(self, item):
        os.makedirs(os.path.dirname(item.cache_path), exist_ok=True)
        for i in range(1, 5):
            self.progress("↓ " + item.media.kind, i, 4)
            await asyncio.sleep(0.15)
        with open(item.cache_path, "wb") as f:
            f.write(b"demo")
        return item.cache_path

    async def set_unread(self, chat, unread, had_mark=False):
        pass

    async def set_pin(self, chat, pinned):
        pass

    async def set_mute(self, chat, muted):
        pass

    async def delete_chat(self, chat):
        pass

    async def user_info(self, chat):
        return [f"Name:      {chat.name}", f"Id:        {chat.id}", f"Status:    {chat.status}",
                "", "Bio:", "Demo user — nothing here is real."]

    async def chat_info(self, chat):
        return [f"Title:     {chat.name}", f"Id:        {chat.id}", f"Type:      {chat.kind}"]
