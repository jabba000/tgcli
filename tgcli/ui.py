"""Curses UI: chat list (left), messages (right), status/input line (bottom)."""
import curses
import logging
import os
import queue
import shlex
import shutil
import subprocess
import tempfile
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from .media import (VoiceRecorder, detached_open, find_viewer, guess_mime, notify,
                    parse_mailcap, pick_file)
from .models import MediaInfo, MsgItem, preview
from .utils import (URL_RE, copy_to_clipboard, cw, fmt_chat_time, fmt_day, fmt_time, human_duration,
                    pad, swidth, truncate, wrap)

log = logging.getLogger("tgcli")

HELP = """\
CHATS
  j k / arrows   move            J K   move 10          g G   top / bottom
  l Enter        open chat       /     search chats     c     contacts
  m  mute/unmute p  pin/unpin    u     mark (un)read    r     mark read
  dd             delete chat / clear history            q     quit

MESSAGES
  j k J K G g    move / 10 / last / first (counts work: 4j)
  i a            write a message          I A   write in $EDITOR
  r R            reply                    e     edit own message
  Space          select + down            Ctrl-Space  select + up
  y              yank selected (also copies text / file path to clipboard)
  p              forward yanked msgs here dd    delete (selected or current)
  l Enter        open media with mailcap viewer / text in pager
  D              download and copy to DOWNLOAD_DIR
  o              open url (list if several)
  S              pick a file and send (type by mime)
  sp sv sa sd    send picture / video / audio / document
  v              record voice msg: Enter = send, Esc = cancel
  u  user info   c  chat info     ! run a command ({file} = media path, else text on stdin)
  ] [            next / previous chat     m  mute   h Esc  back   q  quit

Marks:  ⤓ not downloaded yet   *  selected   N new   U unseen by recipient   E edited
"""


def keyname(k):
    if isinstance(k, int):
        return {
            curses.KEY_UP: "<up>", curses.KEY_DOWN: "<down>", curses.KEY_LEFT: "<left>",
            curses.KEY_RIGHT: "<right>", curses.KEY_HOME: "<home>", curses.KEY_END: "<end>",
            curses.KEY_BACKSPACE: "<bs>", curses.KEY_DC: "<del>", curses.KEY_ENTER: "<cr>",
            curses.KEY_PPAGE: "<pgup>", curses.KEY_NPAGE: "<pgdn>", curses.KEY_RESIZE: "<resize>",
        }.get(k, f"<{k}>")
    return {"\n": "<cr>", "\r": "<cr>", "\x1b": "<esc>", "\x7f": "<bs>", "\b": "<bs>",
            "\x00": "<c-space>", "\t": "<tab>"}.get(k, k)


def clip(s, width):
    out, used = [], 0
    for ch in s:
        c = cw(ch)
        if used + c > width:
            break
        out.append(ch)
        used += c
    return "".join(out)


def sort_key(m):
    return (0, m.id) if m.id > 0 else (1, -m.id)


# ------------------------------------------------------------------ small widgets
class LineEdit:
    def __init__(self, prompt, text="", on_submit=None, on_cancel=None, on_change=None):
        self.prompt, self.text, self.pos = prompt, text, len(text)
        self.on_submit, self.on_cancel, self.on_change = on_submit, on_cancel, on_change

    def handle(self, k):
        t, p = self.text, self.pos
        if k == "<cr>":
            if self.on_submit:
                self.on_submit(t)
            return True
        if k == "<esc>":
            if self.on_cancel:
                self.on_cancel()
            return True
        if k in ("<left>", "\x02"):
            self.pos = max(0, p - 1)
        elif k in ("<right>", "\x06"):
            self.pos = min(len(t), p + 1)
        elif k in ("<home>", "\x01"):
            self.pos = 0
        elif k in ("<end>", "\x05"):
            self.pos = len(t)
        elif k == "<bs>":
            if p:
                self.text, self.pos = t[:p - 1] + t[p:], p - 1
        elif k == "<del>":
            self.text = t[:p] + t[p + 1:]
        elif k == "\x15":  # ctrl-u
            self.text, self.pos = t[p:], 0
        elif k == "\x0b":  # ctrl-k
            self.text = t[:p]
        elif k == "\x17":  # ctrl-w
            i = p
            while i and t[i - 1] == " ":
                i -= 1
            while i and t[i - 1] != " ":
                i -= 1
            self.text, self.pos = t[:i] + t[p:], i
        elif isinstance(k, str) and len(k) == 1 and ord(k) >= 32 and k != "\x7f":
            self.text, self.pos = t[:p] + k + t[p:], p + 1
        else:
            return False
        if self.on_change:
            self.on_change(self.text)
        return False


class Popup:
    def __init__(self, title, lines, on_select=None):
        self.title, self.lines, self.on_select = title, lines, on_select
        self.sel = 0
        self.top = 0
        self.closed = False

    def handle(self, k):
        n = len(self.lines)
        if k in ("q", "<esc>", "h", "<left>"):
            self.closed = True
        elif k in ("j", "<down>"):
            self.sel = min(n - 1, self.sel + 1)
        elif k in ("k", "<up>"):
            self.sel = max(0, self.sel - 1)
        elif k in ("J", "<pgdn>"):
            self.sel = min(n - 1, self.sel + 10)
        elif k in ("K", "<pgup>"):
            self.sel = max(0, self.sel - 10)
        elif k == "g":
            self.sel = 0
        elif k == "G":
            self.sel = max(0, n - 1)
        elif k in ("<cr>", "l", "<right>") and self.on_select:
            self.closed = True
            self.on_select(self.sel)

    def draw(self, app):
        H, W = app.scr.getmaxyx()
        inner = max([swidth(l) for l in self.lines] + [swidth(self.title) + 2, 20])
        w = min(W - 4, inner + 4)
        h = min(H - 3, len(self.lines) + 2)
        y0, x0 = max(0, (H - 1 - h) // 2), max(0, (W - w) // 2)
        vis = h - 2
        self.top = min(self.top, self.sel)
        if self.sel >= self.top + vis:
            self.top = self.sel - vis + 1
        if not self.on_select:  # plain text: scroll without a cursor
            self.top = min(self.sel, max(0, len(self.lines) - vis))
        title = f" {truncate(self.title, w - 6)} "
        app.put(y0, x0, "┌" + "─" * (w - 2) + "┐")
        app.put(y0, x0 + 2, title, curses.A_BOLD)
        for r in range(vis):
            y = y0 + 1 + r
            app.put(y, x0, "│" + " " * (w - 2) + "│")
            i = self.top + r
            if i < len(self.lines):
                attr = curses.A_REVERSE if (self.on_select and i == self.sel) else 0
                app.put(y, x0 + 2, pad(self.lines[i], w - 4) if attr else self.lines[i], attr, w - 4)
        app.put(y0 + h - 1, x0, "└" + "─" * (w - 2) + "┘")


# ------------------------------------------------------------------ the app
class App:
    def __init__(self, cfg, backend):
        self.cfg, self.backend = cfg, backend
        self.scr = None
        self.running = True
        self.dirty = True
        self.mode = "chats"
        self.chats = []
        self.chat_by_id = {}
        self.sel_chat = None
        self.open_id = None
        self.chat_filter = ""
        self.chat_top = 0
        self.msgs = {}
        self.cur_msg = {}
        self.view_top = {}
        self.loading = set()
        self.dl_waiters = {}            # (chat_id, msg_id) -> callbacks to run after download
        self.exhausted = set()
        self.selected = set()           # (chat_id, msg_id)
        self.yanked = None              # (ChatItem, [MsgItem])
        self.input = None
        self.confirm = None
        self.modal = None
        self.recorder = None
        self.reply_to = None
        self.count = ""
        self.pending = ""
        self.status = ""
        self.status_until = 0
        self._tmp = 0
        self.mailcap = parse_mailcap(cfg.MAILCAP_FILE)
        self.keymap = self._build_keymap()

    # ============================================================ setup / main loop
    def setup(self, scr):
        self.scr = scr
        curses.curs_set(0)
        scr.keypad(True)
        scr.timeout(100)
        self._init_colors()
        self.refresh_chats()
        self.set_status("loading chats…  (press ? for help)", 30)

    def run(self, scr):
        self.setup(scr)
        while self.running:
            try:
                self.process_events()
                if self.status and time.time() > self.status_until:
                    self.status = ""
                    self.dirty = True
                if self.recorder:
                    self.dirty = True
                if self.dirty:
                    self.dirty = False
                    self.draw()
                try:
                    k = scr.get_wch()
                except curses.error:
                    continue
                self.dirty = True
                self.on_key(keyname(k))
            except KeyboardInterrupt:
                self.running = False
            except Exception as e:  # keep the UI alive on bugs
                log.exception("ui error")
                self.set_status(f"internal error: {e}", 8)
        if self.recorder:
            self.recorder.cancel()

    def _init_colors(self):
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        n = getattr(curses, "COLORS", 8)
        self.user_attrs = []
        for i, c in enumerate(self.cfg.USERS_COLORS):
            try:
                curses.init_pair(i + 1, c % max(n, 1), -1)
                self.user_attrs.append(curses.color_pair(i + 1))
            except curses.error:
                self.user_attrs.append(0)
        if not self.user_attrs:
            self.user_attrs = [0]
        base = len(self.user_attrs) + 1
        self.attr = {"dim": curses.A_DIM, "media": 0, "err": curses.A_BOLD}
        try:
            curses.init_pair(base, 8 if n >= 16 else 7, -1)
            self.attr["dim"] = curses.color_pair(base) | (0 if n >= 16 else curses.A_DIM)
            curses.init_pair(base + 1, 6, -1)
            self.attr["media"] = curses.color_pair(base + 1)
            curses.init_pair(base + 2, 1, -1)
            self.attr["err"] = curses.color_pair(base + 2) | curses.A_BOLD
        except curses.error:
            pass

    # ============================================================ events from the backend
    def process_events(self):
        while True:
            try:
                ev = self.backend.events.get_nowait()
            except queue.Empty:
                return
            self.dirty = True
            try:
                getattr(self, "ev_" + ev[0])(*ev[1:])
            except Exception:
                log.exception("event %s failed", ev[0])

    def ev_call(self, fn, arg):
        fn(arg)

    def ev_error(self, exc):
        self.set_status(f"error: {exc}", 8)

    def ev_progress(self, label, cur, total):
        pct = int(cur * 100 / total) if total else 0
        self.set_status(f"{label} {pct}%", 2)

    def ev_new_message(self, item):
        chat = self.chat_by_id.get(item.chat_id)
        if chat is None:
            self.refresh_chats()
            return
        lst = self.msgs.get(chat.id)
        if lst is not None:
            if any(m.id == item.id for m in lst):
                return
            if item.outgoing:  # drop the optimistic twin
                for p in lst:
                    if p.state == "pending" and p.text == item.text and not p.media:
                        lst.remove(p)
                        break
            was_last = self.cur_msg.get(chat.id, len(lst) - 1) >= len(lst) - 1
            lst.append(item)
            lst.sort(key=sort_key)
            if was_last:
                self.cur_msg[chat.id] = len(lst) - 1
            self.maybe_autodownload(item)
        self.bump_chat(chat, item)
        viewing = self.mode == "msgs" and self.open_id == chat.id
        if not item.outgoing:
            if viewing and self.cfg.AUTO_READ:
                self.mark_read(chat, force=True)
            else:
                chat.unread_count += 1
            if not chat.muted and not viewing:
                who = item.sender if chat.kind in ("group", "channel") else ""
                notify(self.cfg, chat.name, who, preview(item, 120))
        self.sort_chats()

    def ev_edited(self, item):
        lst = self.msgs.get(item.chat_id) or []
        for i, old in enumerate(lst):
            if old.id == item.id:
                item.path = item.path or old.path
                item.reply_preview = item.reply_preview or old.reply_preview
                lst[i] = item
                break

    def ev_deleted(self, chat_id, ids):
        ids = set(ids)
        for cid, lst in self.msgs.items():
            if chat_id is None or cid == chat_id:
                lst[:] = [m for m in lst if m.id not in ids]
                self.clamp(cid)

    def ev_read(self, chat_id, max_id, outbox):
        c = self.chat_by_id.get(chat_id)
        if not c:
            return
        if outbox:
            c.read_outbox_max_id = max(c.read_outbox_max_id, max_id)
        else:
            c.read_inbox_max_id = max(c.read_inbox_max_id, max_id)
            if max_id >= c.last_msg_id:
                c.unread_count, c.unread_mark = 0, False

    def ev_user_status(self, user_id, online, text):
        for c in self.chats:
            if c.user_id == user_id:
                c.online = online
                c.status = text or c.status

    # ============================================================ chats data
    def refresh_chats(self):
        def done(chats):
            keep = self.sel_chat
            self.chats = chats
            self.chat_by_id = {c.id: c for c in chats}
            self.sort_chats()
            if keep in self.chat_by_id:
                self.sel_chat = keep
            elif chats:
                self.sel_chat = chats[0].id
            self.set_status("")
        self.backend.submit(self.backend.get_chats(self.cfg.MAX_CHATS), done)

    def sort_chats(self):
        self.chats.sort(key=lambda c: (not c.pinned, -(c.last_date.timestamp() if c.last_date else 0)))

    def bump_chat(self, chat, item):
        if item.id >= chat.last_msg_id:
            chat.last_msg_id = item.id
            chat.last_text = item.text
            chat.last_date = item.date
            chat.last_outgoing = item.outgoing
        self.sort_chats()

    def vis_chats(self):
        f = self.chat_filter.lower()
        return [c for c in self.chats if f in c.name.lower()] if f else self.chats

    def chat_index(self, vis=None):
        vis = self.vis_chats() if vis is None else vis
        for i, c in enumerate(vis):
            if c.id == self.sel_chat:
                return i
        return 0

    def sel(self):
        vis = self.vis_chats()
        return vis[self.chat_index(vis)] if vis else None

    def open_obj(self):
        return self.chat_by_id.get(self.open_id)

    # ============================================================ messages data
    def cur_item(self):
        lst = self.msgs.get(self.open_id) or []
        if not lst:
            return None
        i = min(max(self.cur_msg.get(self.open_id, len(lst) - 1), 0), len(lst) - 1)
        return lst[i]

    def clamp(self, cid):
        n = len(self.msgs.get(cid) or [])
        self.cur_msg[cid] = max(0, min(self.cur_msg.get(cid, n - 1), n - 1))

    def merge(self, old, new):
        d = {m.id: m for m in old}
        for m in new:
            d[m.id] = m
        return sorted(d.values(), key=sort_key)

    def open_chat(self, chat):
        self.mode = "msgs"
        self.open_id = chat.id
        self.sel_chat = chat.id
        chat.new_after = chat.read_inbox_max_id
        self.selected.clear()
        if chat.id not in self.msgs:
            self.load_chat(chat)
        else:
            self.cur_msg[chat.id] = len(self.msgs[chat.id]) - 1
            if self.cfg.AUTO_READ:
                self.mark_read(chat)

    def load_chat(self, chat):
        self.msgs[chat.id] = []
        self.loading.add(chat.id)

        def done(items):
            self.loading.discard(chat.id)
            self.msgs[chat.id] = self.merge(self.msgs.get(chat.id, []), items)
            self.cur_msg[chat.id] = len(self.msgs[chat.id]) - 1
            if len(items) < self.cfg.MSGS_LOAD:
                self.exhausted.add(chat.id)
            for it in items[-30:]:
                self.maybe_autodownload(it)
            if self.open_id == chat.id and self.cfg.AUTO_READ:
                self.mark_read(chat)

        def err(e):
            self.loading.discard(chat.id)
            self.msgs.pop(chat.id, None)
            self.set_status(f"cannot load chat: {e}", 8)

        self.backend.submit(self.backend.get_messages(chat, self.cfg.MSGS_LOAD), done, err)

    def load_older(self):
        chat = self.open_obj()
        lst = self.msgs.get(self.open_id)
        if not chat or not lst or chat.id in self.loading or chat.id in self.exhausted:
            return
        self.loading.add(chat.id)
        first = next((m.id for m in lst if m.id > 0), 0)

        def done(items):
            self.loading.discard(chat.id)
            cur = self.cur_item()
            self.msgs[chat.id] = self.merge(self.msgs[chat.id], items)
            if cur:
                self.cur_msg[chat.id] = self.msgs[chat.id].index(cur)
            if len(items) < self.cfg.MSGS_LOAD:
                self.exhausted.add(chat.id)

        def err(e):
            self.loading.discard(chat.id)
            self.set_status(f"cannot load history: {e}", 8)

        self.backend.submit(self.backend.get_messages(chat, self.cfg.MSGS_LOAD, first), done, err)

    def mark_read(self, chat, force=False):
        if not (force or chat.unread_count or chat.unread_mark):
            return
        had = chat.unread_mark
        chat.unread_count, chat.unread_mark = 0, False
        chat.read_inbox_max_id = max(chat.read_inbox_max_id, chat.last_msg_id)
        self.backend.submit(self.backend.set_unread(chat, False, had_mark=had),
                            on_error=lambda e: self.set_status(f"mark read failed: {e}", 5))

    # ============================================================ media
    def maybe_autodownload(self, it):
        m = it.media
        if (not m or not m.downloadable or it.path or it.downloading is not None
                or m.kind not in self.cfg.AUTO_DOWNLOAD):
            return
        if not m.thumb_only and m.size > self.cfg.AUTO_DOWNLOAD_MAX_SIZE:
            return
        self.download(it, quiet=True)

    def download(self, it, quiet=False, then=None):
        if not it.media or not it.media.downloadable:
            return
        key = (it.chat_id, it.id)
        if then:
            self.dl_waiters.setdefault(key, []).append(then)
        if it.downloading is not None:      # already in flight: just wait for it
            return
        it.downloading = 0

        def done(path):
            it.path, it.downloading = path, None
            if not quiet or key in self.dl_waiters:
                self.set_status(f"downloaded {os.path.basename(path)}", 4)
            for cb in self.dl_waiters.pop(key, []):
                cb(it)

        def err(e):
            it.downloading = None
            if not quiet or key in self.dl_waiters:
                self.set_status(f"download failed: {e}", 8)
            self.dl_waiters.pop(key, None)

        self.backend.submit(self.backend.download(it), done, err)

    def suspended(self, fn):
        curses.def_prog_mode()
        curses.endwin()
        try:
            return fn()
        finally:
            curses.reset_prog_mode()
            self.scr.refresh()
            self.scr.clear()
            curses.curs_set(0)
            self.dirty = True

    def run_shell(self, cmd, input_text=None):
        data = input_text.encode() if input_text is not None else None
        self.suspended(lambda: subprocess.run(cmd, shell=isinstance(cmd, str), input=data))

    def open_path(self, it):
        mime = (it.media.mime if it.media else "") or guess_mime(it.path)
        cmd = find_viewer(self.mailcap, mime, it.path)
        if cmd:
            self.run_shell(cmd)
        elif not detached_open(it.path):
            self.set_status(f"no viewer for {mime}: add a mailcap entry", 6)

    # ============================================================ status & input
    def set_status(self, text, secs=5):
        self.status, self.status_until = text, time.time() + secs
        self.dirty = True

    def ask(self, prompt, on_submit, text="", on_change=None, on_cancel=None):
        def submit(t):
            self.input = None
            on_submit(t)

        def cancel():
            self.input = None
            if on_cancel:
                on_cancel()

        self.input = LineEdit(prompt, text, submit, cancel, on_change)

    def ask_confirm(self, question, on_yes):
        self.confirm = (question, on_yes)

    def show_text(self, title, lines):
        self.modal = Popup(title, lines)

    # ============================================================ key handling
    def on_key(self, k):
        if k == "<resize>":
            return
        if self.modal:
            self.modal.handle(k)
            if self.modal.closed:
                self.modal = None
            return
        if self.confirm:
            q, yes = self.confirm
            self.confirm = None
            if k in ("y", "Y"):
                yes()
            return
        if self.recorder:
            if k in ("<cr>", "v"):
                self.stop_recording(send=True)
            elif k in ("<esc>", "q"):
                self.stop_recording(send=False)
            return
        if self.input:
            self.input.handle(k)
            return
        table = self.keymap[self.mode]
        if len(k) == 1 and k.isdigit() and (k != "0" or self.count):
            self.count += k
            return
        seq = self.pending + k
        if seq in table:
            n = int(self.count) if self.count else None
            self.pending = self.count = ""
            table[seq](n)
        elif any(s.startswith(seq) for s in table):
            self.pending = seq
        else:
            self.pending = self.count = ""

    def _build_keymap(self):
        chats = {
            "j": lambda n: self.move_chat(n or 1), "<down>": lambda n: self.move_chat(n or 1),
            "k": lambda n: self.move_chat(-(n or 1)), "<up>": lambda n: self.move_chat(-(n or 1)),
            "J": lambda n: self.move_chat(10 * (n or 1)), "K": lambda n: self.move_chat(-10 * (n or 1)),
            "g": lambda n: self.move_chat(-10 ** 6), "G": lambda n: self.move_chat(10 ** 6),
            "l": self.act_open, "<cr>": self.act_open, "<right>": self.act_open,
            "m": self.act_mute, "p": self.act_pin, "u": self.act_toggle_unread,
            "r": self.act_read, "c": self.act_contacts, "dd": self.act_delete_chat,
            "/": self.act_search, "?": lambda n: self.show_text("Help", HELP.splitlines()),
            "ng": lambda n: self.set_status("creating groups is not implemented yet"),
            "ns": lambda n: self.set_status("secret chats are not supported by MTProto client libs"),
            "<esc>": lambda n: self.clear_filter(), "q": lambda n: self.quit(),
        }
        msgs = {
            "j": lambda n: self.move_msg(n or 1), "<down>": lambda n: self.move_msg(n or 1),
            "k": lambda n: self.move_msg(-(n or 1)), "<up>": lambda n: self.move_msg(-(n or 1)),
            "J": lambda n: self.move_msg(10 * (n or 1)), "K": lambda n: self.move_msg(-10 * (n or 1)),
            "G": lambda n: self.move_msg(10 ** 6), "g": lambda n: self.move_msg(-10 ** 6),
            "D": self.act_save, "l": self.act_open_item, "<cr>": self.act_open_item,
            "<right>": self.act_open_item, "e": self.act_edit,
            " ": lambda n: self.select(1), "<c-space>": lambda n: self.select(-1),
            "y": self.act_yank, "p": self.act_paste, "dd": self.act_delete,
            "i": self.act_insert, "a": self.act_insert, "I": self.act_editor, "A": self.act_editor,
            "v": self.act_record, "r": self.act_reply, "R": self.act_reply,
            "S": lambda n: self.act_send_file(None),
            "sp": lambda n: self.act_send_file("photo"), "sv": lambda n: self.act_send_file("video"),
            "sa": lambda n: self.act_send_file("audio"), "sd": lambda n: self.act_send_file("document"),
            "o": self.act_url, "]": lambda n: self.switch_chat(1), "[": lambda n: self.switch_chat(-1),
            "u": lambda n: self.act_info(True), "c": lambda n: self.act_info(False),
            "m": self.act_mute, "!": self.act_custom,
            "?": lambda n: self.show_text("Help", HELP.splitlines()),
            "h": lambda n: self.back(), "<left>": lambda n: self.back(), "<esc>": lambda n: self.escape(),
            "q": lambda n: self.quit(),
        }
        return {"chats": chats, "msgs": msgs}

    def quit(self):
        self.running = False

    # ------------------------------------------------------------ chat-list actions
    def move_chat(self, d):
        vis = self.vis_chats()
        if vis:
            self.sel_chat = vis[max(0, min(len(vis) - 1, self.chat_index(vis) + d))].id

    def act_open(self, n):
        c = self.sel()
        if c:
            self.open_chat(c)

    def act_mute(self, n):
        c = self.sel() if self.mode == "chats" else self.open_obj()
        if not c:
            return
        c.muted = not c.muted
        self.backend.submit(self.backend.set_mute(c, c.muted),
                            on_error=lambda e: self.set_status(f"mute failed: {e}", 6))
        self.set_status(("muted " if c.muted else "unmuted ") + c.name)

    def act_pin(self, n):
        c = self.sel()
        if not c:
            return
        c.pinned = not c.pinned
        self.sort_chats()
        self.backend.submit(self.backend.set_pin(c, c.pinned),
                            on_error=lambda e: self.set_status(f"pin failed: {e}", 6))

    def act_toggle_unread(self, n):
        c = self.sel()
        if not c:
            return
        if c.unread_count or c.unread_mark:
            self.mark_read(c)
        else:
            c.unread_mark = True
            self.backend.submit(self.backend.set_unread(c, True),
                                on_error=lambda e: self.set_status(f"failed: {e}", 6))

    def act_read(self, n):
        c = self.sel()
        if c:
            self.mark_read(c)

    def act_delete_chat(self, n):
        c = self.sel()
        if not c:
            return

        def yes():
            def done(_):
                self.chats = [x for x in self.chats if x.id != c.id]
                self.chat_by_id.pop(c.id, None)
                self.msgs.pop(c.id, None)
                self.sel_chat = self.chats[0].id if self.chats else None
            self.backend.submit(self.backend.delete_chat(c), done,
                                lambda e: self.set_status(f"delete failed: {e}", 6))
        self.ask_confirm(f"Delete chat '{c.name}' (and its history)? [y/N]", yes)

    def act_contacts(self, n):
        def done(contacts):
            def pick(i):
                c = contacts[i]
                known = self.chat_by_id.get(c.id)
                if not known:
                    self.chats.append(c)
                    self.chat_by_id[c.id] = c
                    known = c
                self.chat_filter = ""
                self.open_chat(known)
            lines = [f"{c.name:<28} {c.status}" for c in contacts] or ["(no contacts)"]
            self.modal = Popup("Contacts", lines, pick if contacts else None)
        self.backend.submit(self.backend.get_contacts(), done,
                            lambda e: self.set_status(f"contacts failed: {e}", 6))

    def act_search(self, n):
        def change(t):
            self.chat_filter = t
            vis = self.vis_chats()
            if vis and self.sel_chat not in {c.id for c in vis}:
                self.sel_chat = vis[0].id
        self.ask("/", lambda t: None, self.chat_filter, change, on_cancel=self.clear_filter)

    def clear_filter(self):
        self.chat_filter = ""

    # ------------------------------------------------------------ message actions
    def move_msg(self, d):
        lst = self.msgs.get(self.open_id)
        if not lst:
            return
        i = self.cur_msg.get(self.open_id, len(lst) - 1) + d
        if i < 0:
            self.load_older()
        self.cur_msg[self.open_id] = max(0, min(len(lst) - 1, i))

    def back(self):
        self.mode = "chats"

    def escape(self):
        if self.selected:
            self.selected.clear()
        else:
            self.back()

    def switch_chat(self, d):
        vis = self.vis_chats()
        if not vis:
            return
        i = next((j for j, c in enumerate(vis) if c.id == self.open_id), 0) + d
        if 0 <= i < len(vis):
            self.open_chat(vis[i])

    def select(self, d):
        it = self.cur_item()
        if not it:
            return
        key = (it.chat_id, it.id)
        self.selected.symmetric_difference_update({key})
        self.move_msg(d)

    def chosen(self):
        lst = self.msgs.get(self.open_id) or []
        sel = [m for m in lst if (m.chat_id, m.id) in self.selected]
        if sel:
            return sel
        it = self.cur_item()
        return [it] if it else []

    def act_yank(self, n):
        items, chat = self.chosen(), self.open_obj()
        if not items or not chat:
            return
        self.yanked = (chat, items)
        if len(items) == 1 and items[0].path:
            text = items[0].path
        else:
            text = "\n".join(m.text for m in items if m.text)
        ok = copy_to_clipboard(text) if text else False
        self.selected.clear()
        self.set_status(f"yanked {len(items)} msg(s)" + ("" if ok else " (no clipboard tool found)"))

    def act_paste(self, n):
        chat = self.open_obj()
        if not self.yanked or not chat:
            self.set_status("nothing yanked")
            return
        src, items = self.yanked
        ids = [m.id for m in items if m.id > 0]
        self.backend.submit(self.backend.forward(chat, src, ids),
                            lambda _: self.set_status(f"forwarded {len(ids)} msg(s)"),
                            lambda e: self.set_status(f"forward failed: {e}", 8))

    def act_delete(self, n):
        items, chat = self.chosen(), self.open_obj()
        if not items or not chat:
            return

        def yes():
            local = [m for m in items if m.id < 0]
            remote = [m.id for m in items if m.id > 0]
            lst = self.msgs[chat.id]
            for m in local:
                lst.remove(m)
            self.selected.clear()
            self.clamp(chat.id)
            if remote:
                self.backend.submit(self.backend.delete(chat, remote),
                                    lambda _: self.ev_deleted(chat.id, remote),
                                    lambda e: self.set_status(f"delete failed: {e}", 8))
        self.ask_confirm(f"Delete {len(items)} message(s) for everybody? [y/N]", yes)

    def act_edit(self, n):
        it, chat = self.cur_item(), self.open_obj()
        if not it or not chat or not it.outgoing or it.id < 0:
            self.set_status("can only edit your own sent messages")
            return

        def go(text):
            if not text.strip() or text == it.text:
                return

            def done(new):
                self.ev_edited(new)
            self.backend.submit(self.backend.edit(chat, it.id, text), done,
                                lambda e: self.set_status(f"edit failed: {e}", 8))
        self.ask("edit> ", go, it.text)

    # ---- sending
    def _add_pending(self, chat, text, media=None, reply_to=None):
        self._tmp -= 1
        me = self.backend.me
        p = MsgItem(id=self._tmp, chat_id=chat.id, date=datetime.now(timezone.utc),
                    sender_id=getattr(me, "id", None), sender=self.backend.me_name, outgoing=True,
                    text=text, reply_to_id=reply_to, media=media, state="pending")
        if reply_to:
            src = next((m for m in self.msgs.get(chat.id, []) if m.id == reply_to), None)
            if src:
                p.reply_preview = f"{src.sender}: {preview(src, 50)}"
        lst = self.msgs.setdefault(chat.id, [])
        lst.append(p)
        self.cur_msg[chat.id] = len(lst) - 1
        return p

    def _track(self, chat, pend, coro, ok=None):
        def done(real):
            lst = self.msgs.get(chat.id, [])
            was_last = self.cur_msg.get(chat.id, 0) >= len(lst) - 1
            if pend in lst:
                lst.remove(pend)
            if real and not any(m.id == real.id for m in lst):
                lst.append(real)
                lst.sort(key=sort_key)
            if real:
                self.bump_chat(chat, real)
            if was_last:
                self.cur_msg[chat.id] = len(lst) - 1
            self.clamp(chat.id)
            if ok:
                self.set_status(ok, 3)

        def err(e):
            pend.state = "failed"
            self.set_status(f"send failed: {e}", 8)

        self.backend.submit(coro, done, err)

    def send_text(self, chat, text, reply_to=None):
        text = text.strip("\n")
        if not text.strip():
            return
        p = self._add_pending(chat, text, reply_to=reply_to)
        self._track(chat, p, self.backend.send_text(chat, text, reply_to))

    def act_insert(self, n):
        chat = self.open_obj()
        if chat:
            self.ask("> ", lambda t: self.send_text(chat, t))

    def act_reply(self, n):
        it, chat = self.cur_item(), self.open_obj()
        if not it or not chat or it.id < 0:
            return
        self.ask(f"reply to {truncate(it.sender, 12)}> ", lambda t: self.send_text(chat, t, it.id))

    def act_editor(self, n):
        chat = self.open_obj()
        if not chat:
            return
        fd, path = tempfile.mkstemp(prefix="tgcli-", suffix=".txt")
        os.close(fd)
        try:
            self.suspended(lambda: subprocess.run(shlex.split(self.cfg.EDITOR) + [path]))
            text = Path(path).read_text()
        finally:
            os.unlink(path)
        self.send_text(chat, text)

    def act_send_file(self, kind):
        chat = self.open_obj()
        if not chat:
            return
        if self.cfg.FILE_PICKER_CMD and shutil.which(shlex.split(self.cfg.FILE_PICKER_CMD)[0]):
            path = self.suspended(lambda: pick_file(self.cfg.FILE_PICKER_CMD))
            if path:
                self.send_path(chat, path, kind)
        else:
            self.ask("file path: ", lambda t: self.send_path(chat, t, kind))

    def send_path(self, chat, path, kind):
        path = os.path.abspath(os.path.expanduser(path.strip()))
        if not os.path.isfile(path):
            self.set_status(f"not a file: {path}", 6)
            return
        mime = guess_mime(path)
        if kind is None:
            top = mime.split("/")[0]
            kind = {"image": "photo", "video": "video", "audio": "audio"}.get(top, "document")
        media = MediaInfo(kind, os.path.basename(path), os.path.getsize(path), mime, downloadable=False)
        p = self._add_pending(chat, "", media)
        self._track(chat, p, self.backend.send_file(chat, path, kind), "file sent")

    # ---- voice
    def act_record(self, n):
        chat = self.open_obj()
        if not chat:
            return
        if not self.cfg.VOICE_RECORD_CMD:
            self.set_status("set VOICE_RECORD_CMD in your config (ffmpeg not found)", 8)
            return
        path = Path(self.cfg.FILES_DIR) / "voice" / f"{int(time.time())}.ogg"
        rec = VoiceRecorder(self.cfg.VOICE_RECORD_CMD, path)
        try:
            rec.start()
        except Exception as e:
            self.set_status(f"cannot record: {e}", 8)
            return
        self.recorder = rec

    def stop_recording(self, send):
        rec, self.recorder = self.recorder, None
        chat = self.open_obj()
        if not send or not chat:
            rec.cancel()
            self.set_status("recording cancelled")
            return
        dur = rec.stop()
        if dur < 1 or not os.path.exists(rec.path):
            rec.cancel()
            self.set_status("recording too short")
            return
        media = MediaInfo("voice", "", os.path.getsize(rec.path), "audio/ogg", int(dur), downloadable=False)
        p = self._add_pending(chat, "", media)
        self._track(chat, p, self.backend.send_voice(chat, rec.path, dur), "voice message sent")

    # ---- open / save / url / info / custom
    def act_open_item(self, n):
        it = self.cur_item()
        if not it:
            return
        m = it.media
        if m and m.downloadable:
            if it.path and os.path.exists(it.path):
                self.open_path(it)
            else:
                self.set_status("downloading…", 3)
                self.download(it, then=self.open_path)
        elif m and m.kind == "geo":
            webbrowser.open("https://www.openstreetmap.org/?mlat={0}&mlon={1}".format(*m.extra.split(",")))
        elif it.text:
            self.run_shell(os.environ.get("PAGER", "less"), it.text)

    def act_save(self, n):
        it = self.cur_item()
        if not it or not it.media or not it.media.downloadable:
            return

        def copy(done_item):
            dest_dir = Path(self.cfg.DOWNLOAD_DIR).expanduser()
            dest_dir.mkdir(parents=True, exist_ok=True)
            name = it.media.name or os.path.basename(done_item.path)
            dest = dest_dir / name
            shutil.copy(done_item.path, dest)
            self.set_status(f"saved to {dest}", 6)
        if it.path and os.path.exists(it.path):
            copy(it)
        else:
            self.set_status("downloading…", 3)
            self.download(it, then=copy)

    def act_url(self, n):
        it = self.cur_item()
        urls = URL_RE.findall(it.text) if it else []
        if not urls:
            self.set_status("no urls in this message")
        elif len(urls) == 1:
            webbrowser.open(urls[0])
        else:
            self.modal = Popup("Open url", urls, lambda i: webbrowser.open(urls[i]))

    def act_info(self, user):
        chat = self.open_obj()
        if not chat:
            return
        coro = self.backend.user_info(chat) if user else self.backend.chat_info(chat)
        self.backend.submit(coro, lambda lines: self.show_text(chat.name, lines),
                            lambda e: self.set_status(f"info failed: {e}", 6))

    def act_custom(self, n):
        it = self.cur_item()
        if not it:
            return

        def go(cmd):
            if "{file}" in cmd:
                if not it.path:
                    self.set_status("download the file first (D)")
                    return
                self.run_shell(cmd.replace("{file}", shlex.quote(it.path)))
            else:
                self.run_shell(cmd, it.text)
        self.ask("cmd ({file} or text on stdin)> ", go)

    # ============================================================ drawing
    def put(self, y, x, s, attr=0, maxw=None):
        H, W = self.scr.getmaxyx()
        if y < 0 or y >= H or x >= W or x < 0:
            return
        lim = W - x if maxw is None else min(maxw, W - x)
        try:
            self.scr.addstr(y, x, clip(s, lim), attr)
        except curses.error:
            pass

    def draw(self):
        scr = self.scr
        scr.erase()
        H, W = scr.getmaxyx()
        if H < 6 or W < 40:
            self.put(0, 0, "terminal too small")
            scr.refresh()
            return
        lw = min(max(20, int(W * self.cfg.CHATS_WIDTH)), W // 2)
        ph = H - 1
        self.draw_chats(lw, ph)
        for y in range(ph):
            self.put(y, lw, "│", self.attr["dim"])
        self.draw_msgs(lw + 1, W - lw - 1, ph)
        self.draw_status(H - 1, W)
        if self.modal:
            self.modal.draw(self)
        curses.curs_set(1 if (self.input and not self.modal) else 0)
        if self.input and not self.modal:
            self._place_cursor(H - 1, W)
        scr.refresh()

    def chat_flags(self, c):
        f, fl = [], self.cfg.CHAT_FLAGS
        if c.online:
            f.append(fl.get("online", ""))
        if c.pinned:
            f.append(fl.get("pinned", ""))
        if c.muted:
            f.append(fl.get("muted", ""))
        if c.unread_mark:
            f.append(fl.get("unread", ""))
        if c.last_outgoing and c.last_msg_id:
            f.append(fl.get("seen", "") if c.last_msg_id <= c.read_outbox_max_id else fl.get("unseen", ""))
        return "".join(f)

    def draw_chats(self, w, h):
        vis = self.vis_chats()
        idx = self.chat_index(vis)
        if idx < self.chat_top:
            self.chat_top = idx
        elif idx >= self.chat_top + h:
            self.chat_top = idx - h + 1
        if not vis:
            self.put(0, 1, "no chats" if not self.chat_filter else "no matches", self.attr["dim"])
        for r in range(h):
            i = self.chat_top + r
            if i >= len(vis):
                break
            c = vis[i]
            right = self.chat_flags(c)
            if c.unread_count:
                right += f" ({c.unread_count})"
            left = f"{fmt_chat_time(c.last_date):>6} "
            name_w = w - swidth(left) - swidth(right) - 2
            line = left + pad(c.name, max(name_w, 4)) + " " + right
            attr = 0
            if i == idx:
                attr = curses.A_REVERSE if self.mode == "chats" else curses.A_BOLD
                self.put(r, 0, " " * w, attr)
            elif c.unread_count and not c.muted:
                attr = curses.A_BOLD
            self.put(r, 0, line, attr, w)

    def user_attr(self, sid):
        return self.user_attrs[abs(sid or 0) % len(self.user_attrs)]

    def msg_flags(self, chat, m):
        fl, f = self.cfg.MSG_FLAGS, []
        if (m.chat_id, m.id) in self.selected:
            f.append(fl.get("selected", "*"))
        if m.fwd_from:
            f.append(fl.get("forwarded", "F"))
        if not m.outgoing and chat.new_after and m.id > chat.new_after:
            f.append(fl.get("new", "N"))
        if m.edited:
            f.append(fl.get("edited", "E"))
        if m.state == "pending":
            f.append(fl.get("pending", "..."))
        elif m.state == "failed":
            f.append(fl.get("failed", "✗"))
        elif m.outgoing and m.id > 0:
            f.append(fl.get("seen", "") if m.id <= chat.read_outbox_max_id else fl.get("unseen", ""))
        return "".join(f)

    def build_lines(self, chat, items, width):
        """-> (lines, spans). line = (segments, msg_index|None); segment = (text, attr)."""
        dim = self.attr["dim"]
        lines, spans = [], {}
        nw = max(4, min(12, width // 5, max((swidth(m.sender) for m in items), default=4)))
        pw = 5 + 1 + nw + 1
        bw = max(10, width - pw - 8)
        last_day = None
        for idx, m in enumerate(items):
            day = fmt_day(m.date)
            if day != last_day:
                last_day = day
                lines.append(([(("── " + day + " ──").center(width), dim)], None))
            body = []
            if m.fwd_from:
                body.append((truncate("↪ forwarded from " + m.fwd_from, bw), dim))
            if m.reply_to_id:
                body.append((truncate("↳ " + (m.reply_preview or "…"), bw), dim))
            if m.media:
                label = m.media.label()
                if m.media.downloadable and not m.path:
                    label += " (" + (f"{m.downloading}%" if m.downloading is not None else "⤓") + ")"
                if m.media.kind in ("voice", "video_note") and not m.media.duration:
                    label = label.replace("  ", " ")
                body.append((truncate(label, bw), self.attr["media"]))
            for l in (wrap(m.text, bw) if m.text else []):
                body.append((l, 0))
            if not body:
                body.append(("", 0))
            flags = self.msg_flags(chat, m)
            start = len(lines)
            name_attr = self.user_attr(m.sender_id) | (curses.A_BOLD if m.outgoing else 0)
            for j, (txt, attr) in enumerate(body):
                if j == 0:
                    segs = [(fmt_time(m.date), dim), (" ", 0), (pad(m.sender, nw), name_attr),
                            (" ", 0), (pad(txt, bw), attr)]
                    if m.state == "failed":
                        segs.append((" " + flags, self.attr["err"]))
                    else:
                        segs.append((" " + flags, dim))
                else:
                    segs = [(" " * pw, 0), (txt, attr)]
                lines.append((segs, idx))
            spans[idx] = (start, len(lines))
        return lines, spans

    def draw_msgs(self, x0, w, h):
        chat = self.open_obj()
        if not chat:
            self.put(1, x0 + 2, "Select a chat and press l / Enter", self.attr["dim"])
            return
        status = chat.status if chat.kind in ("user", "bot") else chat.kind
        self.put(0, x0 + 1, chat.name, curses.A_BOLD, w - 2)
        self.put(0, x0 + 2 + swidth(chat.name), status, self.attr["dim"], w)
        self.put(1, x0, "─" * w, self.attr["dim"])
        items = self.msgs.get(chat.id) or []
        area = h - 2
        if not items:
            self.put(2, x0 + 1, "loading…" if chat.id in self.loading else "no messages", self.attr["dim"])
            return
        lines, spans = self.build_lines(chat, items, w - 1)
        cur = min(max(self.cur_msg.get(chat.id, len(items) - 1), 0), len(items) - 1)
        s, e = spans[cur]
        top = self.view_top.get(chat.id, 0)
        if cur == len(items) - 1 and self.mode == "msgs":
            top = max(0, len(lines) - area)
        elif e - s >= area or s < top:
            top = s
        elif e > top + area:
            top = e - area
        top = max(0, min(top, max(0, len(lines) - area)))
        self.view_top[chat.id] = top
        for r in range(area):
            li = top + r
            if li >= len(lines):
                break
            segs, idx = lines[li]
            hl = (idx == cur and self.mode == "msgs")
            y, x = 2 + r, x0 + 1
            if hl:
                self.put(y, x0, " " * w, curses.A_REVERSE)
            for txt, attr in segs:
                self.put(y, x, txt, attr | (curses.A_REVERSE if hl else 0), x0 + w - x)
                x += swidth(txt)

    def draw_status(self, y, W):
        attr = curses.A_REVERSE
        self.put(y, 0, " " * W, attr)
        right = self.count + self.pending
        if self.input:
            self.put(y, 0, " " * W, 0)
            self.put(y, 0, self._input_view(W)[0], 0)
            return
        if self.confirm:
            text = self.confirm[0]
        elif self.recorder:
            text = f" ● REC {human_duration(self.recorder.elapsed)}   Enter/v: send   Esc/q: cancel"
        elif self.status:
            text = " " + self.status
        elif self.mode == "msgs":
            text = " h:back  i:write  r:reply  l:open  v:voice  S:send file  ?:help"
        else:
            text = " l:open  /:search  m:mute  p:pin  c:contacts  ?:help  q:quit"
        self.put(y, 0, truncate(text, W - len(right) - 2), attr)
        self.put(y, W - len(right) - 1, right, attr)

    def _input_view(self, W):
        e = self.input
        pw = swidth(e.prompt)
        avail = max(5, W - pw - 1)
        start = 0
        while swidth(e.text[start:e.pos]) >= avail:
            start += 1
        shown = clip(e.text[start:], avail)
        return e.prompt + shown, pw + swidth(e.text[start:e.pos])

    def _place_cursor(self, y, W):
        try:
            self.scr.move(y, min(W - 1, self._input_view(W)[1]))
        except curses.error:
            pass
