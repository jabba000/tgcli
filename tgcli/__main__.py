import argparse
import curses
import getpass
import logging
import os
import sys

from . import __version__
from .config import CONFIG_DIR, load_config
from .utils import cleanup_old_files

NEED_CREDS = f"""\
tgcli needs your own Telegram API credentials (free, takes a minute):
  1. open https://my.telegram.org -> "API development tools" and create an app
  2. put them in {CONFIG_DIR}/conf.py :

       API_ID = 123456
       API_HASH = "0123456789abcdef0123456789abcdef"
       PHONE = "+421900000000"      # optional, otherwise you'll be asked
"""


def login(backend, cfg):
    if backend.call(backend.connect()):
        return
    phone = cfg.PHONE or input("Phone number (international format, +...): ").strip()
    backend.call(backend.send_code(phone))
    for attempt in range(3):
        code = input("Login code (sent to your Telegram app): ").strip()
        try:
            backend.call(backend.sign_in(phone=phone, code=code))
            return
        except Exception as e:
            name = type(e).__name__
            if name == "SessionPasswordNeededError":
                for _ in range(3):
                    pw = getpass.getpass("2FA password: ")
                    try:
                        backend.call(backend.sign_in(password=pw))
                        return
                    except Exception as e2:
                        print("Login failed:", type(e2).__name__)
                sys.exit(1)
            if name in ("PhoneCodeInvalidError", "PhoneCodeEmptyError"):
                print("Wrong code, try again.")
                continue
            print("Login failed:", name, e)
            sys.exit(1)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(prog="tgcli", description="Telegram terminal client")
    ap.add_argument("--demo", action="store_true", help="run with fake data (no login)")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()

    cfg = load_config()
    logging.basicConfig(
        filename=os.path.join(cfg.LOG_PATH, "tgcli.log"), level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cleanup_old_files(cfg.FILES_DIR, cfg.KEEP_MEDIA)

    if args.demo:
        from .demo import DemoBackend
        backend = DemoBackend(cfg)
    else:
        if not cfg.API_ID or not cfg.API_HASH:
            print(NEED_CREDS)
            sys.exit(1)
        try:
            from .backend import TelegramBackend
        except ImportError:
            print("Telethon is not installed: pip install telethon")
            sys.exit(1)
        backend = TelegramBackend(cfg)
        login(backend, cfg)
        backend.call(backend.finish_login())

    from .ui import App
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(App(cfg, backend).run)
    finally:
        if not args.demo:
            try:
                backend.call(backend.disconnect(), timeout=5)
            except Exception:
                pass


if __name__ == "__main__":
    main()
