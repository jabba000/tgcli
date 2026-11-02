# tgcli

Telegram terminal client in the spirit of [paul-nameless/tg](https://github.com/paul-nameless/tg):
curses UI, vi-style keys, mailcap for media, plain-Python config.
Built on **Telethon** (pure-Python MTProto), so there is no TDLib to compile.

```
 21:50 Alice      ● (2) │ Alice online
 15:45 Bob          ✓   │──────────────────────────────────────
 11:40 Dev Team    (5)  │ 19:12 Alice Check https://… and
31 Oct Tech News    M   │ 19:17 You   Yes, we're building a CLI …  U
                        │ 20:20 Alice [photo]
                        │ 21:20 Alice [voice] 0:14 (39.1 KB)     N
```

## Quick start

```bash
pip install -e .            # needs Python >= 3.9
pip install cryptg hachoir  # optional: faster transfers / audio+video metadata on upload
tgcli --demo                # try the UI with fake data, no login
```

For a real account you need your own API credentials (free): <https://my.telegram.org> → *API development tools*.

`~/.config/tgcli/conf.py`:
```python
API_ID = 123456
API_HASH = "0123456789abcdef0123456789abcdef"
PHONE = "+421900000000"        # optional
```
Then run `tgcli`; the first start asks for the login code (and 2FA password if set).
The session is stored in `~/.local/share/tgcli/session.session` (mode 600) — treat it like a password.

## Features (milestone 1)

- Chat list: pinned/muted/unread flags, online dot, seen/unseen ticks, search (`/`), contacts (`c`)
- History with lazy loading of older messages, replies, forwards, edit marks, date separators
- Send, reply, edit, delete, yank + forward, compose in `$EDITOR`
- Media: photos, video, voice, video notes, audio, documents, stickers, GIFs
  - opened through **mailcap** (own parser — stdlib `mailcap` is gone in Python 3.13), falls back to `xdg-open`/`open`
  - configurable auto-download (default: photos, voice, video notes, stickers ≤ 5 MB)
  - send pictures/videos/audio/documents with a file picker (ranger / nnn / fzf, or type a path)
- Voice messages: `v` records with ffmpeg (Opus/Ogg), Enter sends as a real voice note
- Desktop notifications (`notify-send`, `terminal-notifier`, or your own `NOTIFY_CMD`) for unmuted chats you are not looking at
- Live updates: new/edited/deleted messages, read receipts, online status

## Keys

| Chats | | Messages | |
|---|---|---|---|
| `j k J K g G` | move (counts: `4j`) | `j k J K g G` | move / 10 / first / last |
| `l` `Enter` | open chat | `i a` / `I A` | write / write in `$EDITOR` |
| `m` `p` | mute / pin | `r R` `e` | reply / edit |
| `u` `r` | toggle unread / mark read | `Space` `Ctrl-Space` | select + down / up |
| `c` | contacts | `y` `p` | yank / forward here |
| `dd` | delete chat | `dd` | delete (selected or current) |
| `/` | search | `l` `D` `o` | open / download+save / open url |
| `?` `q` | help / quit | `S` `sp sv sa sd` | send file / picture / video / audio / document |
| | | `v` | record voice (Enter send, Esc cancel) |
| | | `u` `c` `!` | user info / chat info / custom command |
| | | `] [` `h` | next / prev chat, back |

## Config

Everything in `tgcli/config.py` can be overridden in `conf.py` (same idea as `tg`):
`NOTIFY_CMD`, `VOICE_RECORD_CMD`, `FILE_PICKER_CMD`, `MAILCAP_FILE`, `DOWNLOAD_DIR`, `KEEP_MEDIA`,
`AUTO_DOWNLOAD`, `CHAT_FLAGS`, `MSG_FLAGS`, `USERS_COLORS`, `CHATS_WIDTH`, `EDITOR`, …

Example `~/.config/mailcap`:
```
video/*; mpv "%s"
audio/*; mpv --no-video "%s"
image/*; qview "%s"
image/webp; mpv "%s"
text/*; less "%s"
```

## Not implemented / known gaps

- **Secret chats** — not available through MTProto client libraries like Telethon (`tg`/TDLib has them).
- Creating groups (`ng`), bot keyboards, global search, message search inside a chat, polls (shown read-only), animated stickers (only the preview frame).
- Archived chats are hidden.

## Layout

```
tgcli/config.py    defaults + conf.py loader        tgcli/media.py    mailcap, voice recorder, picker, notify
tgcli/backend.py   Telethon calls + live events     tgcli/ui.py       curses UI, keymaps, popups
tgcli/runtime.py   asyncio thread + event queue     tgcli/demo.py     fake backend for --demo / UI hacking
tgcli/models.py    ChatItem / MsgItem / MediaInfo   tests/            pure-logic tests
```
The UI never touches Telethon objects directly: the backend runs on its own asyncio thread and
pushes results/updates into a queue that the curses loop drains, so all UI state lives on one thread.
