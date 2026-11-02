"""Telethon backend. Everything here runs on the asyncio thread; the UI only sees the
plain dataclasses from models.py plus events pushed onto `self.events`."""
import asyncio
import logging
import os
import re
import time
from datetime import datetime

from telethon import TelegramClient, events, utils
from telethon.tl import functions, types

from . import __version__
from .models import ChatItem, MediaInfo, MsgItem, preview
from .runtime import AsyncRunner
from .utils import clean

log = logging.getLogger("tgcli")


def _ts(x):
    if x is None:
        return 0
    return x.timestamp() if isinstance(x, datetime) else float(x)


def _text_of(x):
    """Newer layers wrap some strings (poll question...) in TextWithEntities."""
    return getattr(x, "text", x) or ""


class TelegramBackend(AsyncRunner):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.me = None
        self.me_name = "me"
        self.client = None
        self._sem = None
        self.call(self._init())

    async def _init(self):
        self._sem = asyncio.Semaphore(3)
        self.client = TelegramClient(
            self.cfg.SESSION_PATH, self.cfg.API_ID, self.cfg.API_HASH,
            device_model="tgcli", app_version=__version__)

    # ------------------------------------------------------------ login (blocking, pre-curses)
    async def connect(self):
        await self.client.connect()
        return await self.client.is_user_authorized()

    async def send_code(self, phone):
        await self.client.send_code_request(phone)

    async def sign_in(self, phone=None, code=None, password=None):
        await self.client.sign_in(phone=phone, code=code, password=password)

    async def finish_login(self):
        self.me = await self.client.get_me()
        self.me_name = utils.get_display_name(self.me) or "me"
        c = self.client
        c.add_event_handler(self._on_new, events.NewMessage())
        c.add_event_handler(self._on_edit, events.MessageEdited())
        c.add_event_handler(self._on_deleted, events.MessageDeleted())
        c.add_event_handler(self._on_read, events.MessageRead())
        c.add_event_handler(self._on_user, events.UserUpdate())
        try:
            os.chmod(self.cfg.SESSION_PATH + ".session", 0o600)
        except OSError:
            pass

    async def disconnect(self):
        await self.client.disconnect()

    # ------------------------------------------------------------ live events
    async def _on_new(self, ev):
        try:
            self.events.put(("new_message", await self.convert(ev.message)))
        except Exception:
            log.exception("new message handler")

    async def _on_edit(self, ev):
        try:
            self.events.put(("edited", await self.convert(ev.message)))
        except Exception:
            log.exception("edit handler")

    async def _on_deleted(self, ev):
        self.events.put(("deleted", ev.chat_id, list(ev.deleted_ids)))

    async def _on_read(self, ev):
        self.events.put(("read", ev.chat_id, ev.max_id, bool(ev.outbox)))

    async def _on_user(self, ev):
        try:
            if ev.online:
                online, text = True, "online"
            else:
                online = False
                text = self._status_from_event(ev)
            self.events.put(("user_status", ev.user_id, online, text))
        except Exception:
            log.exception("user update handler")

    @staticmethod
    def _status_from_event(ev):
        if getattr(ev, "last_seen", None):
            return "last seen " + ev.last_seen.astimezone().strftime("%d %b %H:%M")
        if getattr(ev, "recently", False):
            return "last seen recently"
        if getattr(ev, "within_weeks", False):
            return "last seen within a week"
        if getattr(ev, "within_months", False):
            return "last seen within a month"
        return ""

    # ------------------------------------------------------------ conversion
    @staticmethod
    def _user_status(u):
        s = getattr(u, "status", None)
        if isinstance(s, types.UserStatusOnline):
            return True, "online"
        if isinstance(s, types.UserStatusOffline):
            t = datetime.fromtimestamp(_ts(s.was_online))
            return False, "last seen " + t.strftime("%d %b %H:%M")
        if isinstance(s, types.UserStatusRecently):
            return False, "last seen recently"
        if isinstance(s, types.UserStatusLastWeek):
            return False, "last seen within a week"
        if isinstance(s, types.UserStatusLastMonth):
            return False, "last seen within a month"
        return False, ""

    def _media(self, m):
        if m.media is None or isinstance(m.media, types.MessageMediaWebPage):
            return None
        f = m.file
        name = (f.name if f else "") or ""
        size = int((f.size if f else 0) or 0)
        mime = (f.mime_type if f else "") or ""
        dur = int((f.duration if f else 0) or 0)
        if m.sticker:
            animated = mime in ("application/x-tgsticker", "video/webm")
            return MediaInfo("sticker", name, size, "image/webp" if animated else mime, 0,
                             extra=(getattr(f, "emoji", "") or ""), thumb_only=animated)
        if m.voice:
            return MediaInfo("voice", name, size, mime, dur)
        if m.video_note:
            return MediaInfo("video_note", name, size, mime, dur)
        if m.gif:
            return MediaInfo("gif", name, size, mime, dur)
        if m.video:
            return MediaInfo("video", name, size, mime, dur)
        if m.audio:
            title = name or ""
            return MediaInfo("audio", title, size, mime, dur)
        if m.photo:
            return MediaInfo("photo", "", size, "image/jpeg")
        if m.document:
            return MediaInfo("document", name, size, mime or "application/octet-stream")
        if m.geo:
            return MediaInfo("geo", extra=f"{m.geo.lat:.5f},{m.geo.long:.5f}", downloadable=False)
        if m.contact:
            c = m.contact
            return MediaInfo("contact", extra=f"{c.first_name} {c.last_name or ''} {c.phone_number}".strip(),
                             downloadable=False)
        if m.poll:
            return MediaInfo("poll", extra=_text_of(m.poll.poll.question), downloadable=False)
        return MediaInfo("unsupported", downloadable=False)

    def _cache_path(self, m, media):
        if media is None or not media.downloadable:
            return None
        f = m.file
        ext = (f.ext if f else "") or ""
        if media.thumb_only:
            ext = ".webp"
        elif not ext and media.kind == "photo":
            ext = ".jpg"
        if media.kind in ("document", "audio") and media.name:
            safe = re.sub(r"[^\w.\- ]", "_", media.name)[:80]
            fname = f"{m.id}_{safe}"
        else:
            fname = f"{m.id}{ext}"
        return os.path.join(self.cfg.FILES_DIR, str(m.chat_id), fname)

    @staticmethod
    def _fwd_name(m):
        f = m.forward
        if not f:
            return None
        for ent in (getattr(f, "sender", None), getattr(f, "chat", None)):
            if ent:
                return utils.get_display_name(ent)
        return getattr(getattr(f, "original_fwd", None), "from_name", None) or "unknown"

    async def convert(self, m):
        try:
            sender = await m.get_sender()
        except Exception:
            sender = None
        name = utils.get_display_name(sender) if sender else ""
        if not name and m.chat:
            name = utils.get_display_name(m.chat)
        text = clean(m.message or "")
        if m.action is not None and not text:
            text = "[" + type(m.action).__name__.replace("MessageAction", "") + "]"
        media = self._media(m)
        item = MsgItem(
            id=m.id, chat_id=m.chat_id, date=m.date, sender_id=m.sender_id, sender=name,
            outgoing=bool(m.out), text=text, edited=bool(m.edit_date),
            reply_to_id=(m.reply_to.reply_to_msg_id if m.reply_to else None),
            fwd_from=self._fwd_name(m), media=media, cache_path=self._cache_path(m, media), raw=m)
        if item.cache_path and os.path.exists(item.cache_path):
            item.path = item.cache_path
        return item

    async def _resolve_replies(self, entity, items):
        by_id = {i.id: i for i in items}
        need = list({i.reply_to_id for i in items if i.reply_to_id and i.reply_to_id not in by_id})
        fetched = {}
        if need:
            try:
                for r in await self.client.get_messages(entity, ids=need):
                    if r:
                        fetched[r.id] = await self.convert(r)
            except Exception:
                log.exception("resolve replies")
        for i in items:
            if not i.reply_to_id:
                continue
            src = by_id.get(i.reply_to_id) or fetched.get(i.reply_to_id)
            i.reply_preview = f"{src.sender}: {preview(src, 50)}" if src else "(deleted message)"

    def _chat_from_entity(self, e, name=None):
        if isinstance(e, types.User):
            kind = "bot" if e.bot else "user"
        elif isinstance(e, types.Channel) and not e.megagroup:
            kind = "channel"
        else:
            kind = "group"
        c = ChatItem(id=utils.get_peer_id(e), name=name or utils.get_display_name(e) or "?",
                     entity=e, kind=kind)
        if isinstance(e, types.User):
            c.user_id = e.id
            c.online, c.status = self._user_status(e)
            if e.is_self:
                c.name = "Saved Messages"
        return c

    # ------------------------------------------------------------ chats
    async def get_chats(self, limit=200):
        chats = []
        async for d in self.client.iter_dialogs(limit=limit, folder=0):
            c = self._chat_from_entity(d.entity, d.name)
            raw = d.dialog
            c.pinned = bool(d.pinned)
            c.unread_count = d.unread_count or 0
            c.unread_mark = bool(getattr(raw, "unread_mark", False))
            c.read_inbox_max_id = getattr(raw, "read_inbox_max_id", 0) or 0
            c.read_outbox_max_id = getattr(raw, "read_outbox_max_id", 0) or 0
            c.muted = _ts(getattr(raw.notify_settings, "mute_until", None)) > time.time()
            c.last_date = d.date
            if d.message:
                c.last_msg_id = d.message.id
                c.last_outgoing = bool(d.message.out)
                c.last_text = (d.message.message or "")[:80]
            chats.append(c)
        return chats

    async def get_contacts(self):
        res = await self.client(functions.contacts.GetContactsRequest(hash=0))
        out = [self._chat_from_entity(u) for u in res.users if isinstance(u, types.User)]
        return sorted(out, key=lambda c: c.name.lower())

    async def get_messages(self, chat, limit=50, max_id=0):
        msgs = await self.client.get_messages(chat.entity, limit=limit, max_id=max_id)
        items = [await self.convert(m) for m in reversed(list(msgs)) if m]
        await self._resolve_replies(chat.entity, items)
        return items

    # ------------------------------------------------------------ sending
    async def send_text(self, chat, text, reply_to=None):
        m = await self.client.send_message(chat.entity, text, reply_to=reply_to, parse_mode=None)
        return await self.convert(m)

    async def send_file(self, chat, path, kind="document", reply_to=None):
        label = "↑ " + os.path.basename(path)
        kw = {}
        if kind == "document":
            kw["force_document"] = True
        elif kind == "video":
            kw["supports_streaming"] = True
        m = await self.client.send_file(
            chat.entity, path, reply_to=reply_to,
            progress_callback=lambda c, t: self.progress(label, c, t), **kw)
        return await self.convert(m)

    async def send_voice(self, chat, path, duration, reply_to=None):
        attrs = [types.DocumentAttributeAudio(duration=int(duration), voice=True)]
        m = await self.client.send_file(chat.entity, path, voice_note=True, attributes=attrs,
                                        reply_to=reply_to)
        return await self.convert(m)

    async def edit(self, chat, msg_id, text):
        m = await self.client.edit_message(chat.entity, msg_id, text, parse_mode=None)
        return await self.convert(m)

    async def delete(self, chat, ids):
        await self.client.delete_messages(chat.entity, ids, revoke=True)

    async def forward(self, dst, src, ids):
        await self.client.forward_messages(dst.entity, ids, from_peer=src.entity)

    # ------------------------------------------------------------ media
    async def download(self, item):
        if item.path and os.path.exists(item.path):
            return item.path
        path = item.cache_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        label = "↓ " + (item.media.name or item.media.kind)
        tmp = path + ".part"
        kw = {"thumb": -1} if item.media.thumb_only else {}
        async with self._sem:
            try:
                await item.raw.download_media(
                    file=tmp, progress_callback=lambda c, t: self.progress(label, c, t), **kw)
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        return path

    # ------------------------------------------------------------ chat actions
    async def _input_peer(self, chat):
        return await self.client.get_input_entity(chat.entity)

    async def set_unread(self, chat, unread, had_mark=False):
        if unread:
            peer = types.InputDialogPeer(await self._input_peer(chat))
            await self.client(functions.messages.MarkDialogUnreadRequest(peer=peer, unread=True))
        else:
            await self.client.send_read_acknowledge(chat.entity, clear_mentions=True)
            if had_mark:
                peer = types.InputDialogPeer(await self._input_peer(chat))
                await self.client(functions.messages.MarkDialogUnreadRequest(peer=peer, unread=False))

    async def set_pin(self, chat, pinned):
        peer = types.InputDialogPeer(await self._input_peer(chat))
        await self.client(functions.messages.ToggleDialogPinRequest(peer=peer, pinned=pinned))

    async def set_mute(self, chat, muted):
        peer = types.InputNotifyPeer(await self._input_peer(chat))
        settings = types.InputPeerNotifySettings(mute_until=2 ** 31 - 1 if muted else 0)
        await self.client(functions.account.UpdateNotifySettingsRequest(peer=peer, settings=settings))

    async def delete_chat(self, chat):
        await self.client.delete_dialog(chat.entity)

    # ------------------------------------------------------------ info popups
    async def user_info(self, chat):
        e = chat.entity
        lines = [f"Name:      {chat.name}", f"Id:        {chat.id}"]
        if not isinstance(e, types.User):
            return await self.chat_info(chat)
        if e.username:
            lines.append(f"Username:  @{e.username}")
        if e.phone:
            lines.append(f"Phone:     +{e.phone}")
        lines.append(f"Status:    {self._user_status(e)[1] or 'unknown'}")
        try:
            full = await self.client(functions.users.GetFullUserRequest(e))
            about = getattr(full.full_user, "about", None)
            if about:
                lines += ["", "Bio:", about]
        except Exception:
            log.exception("full user")
        return lines

    async def chat_info(self, chat):
        e = chat.entity
        lines = [f"Title:     {chat.name}", f"Id:        {chat.id}", f"Type:      {chat.kind}"]
        un = getattr(e, "username", None)
        if un:
            lines.append(f"Username:  @{un}")
        if not isinstance(e, types.User):
            try:
                total = (await self.client.get_participants(e, limit=0)).total
                lines.append(f"Members:   {total}")
            except Exception:
                pass
        lines.append(f"Muted:     {'yes' if chat.muted else 'no'}")
        lines.append(f"Pinned:    {'yes' if chat.pinned else 'no'}")
        return lines
