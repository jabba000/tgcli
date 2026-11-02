"""Pure-logic tests (no Telegram, no curses):  python -m pytest  or  python tests/test_core.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tgcli.media import find_viewer, parse_mailcap
from tgcli.utils import swidth, truncate, wrap, human_size, clean


def test_wrap_respects_cell_width():
    for line in wrap("你好，世界！これはテストです。 plain words here", 10):
        assert swidth(line) <= 10


def test_wrap_hard_breaks_long_tokens_and_keeps_newlines():
    out = wrap("x" * 25 + "\nshort", 10)
    assert out == ["x" * 10, "x" * 10, "x" * 5, "short"]


def test_truncate_adds_ellipsis_within_width():
    t = truncate("abcdefghij", 5)
    assert swidth(t) <= 5 and t.endswith("…")


def test_clean_strips_control_chars():
    assert clean("a\x00b\x1bc\td") == "abc    d"


def test_human_size():
    assert human_size(1536) == "1.5 KB"


def test_mailcap_exact_beats_wildcard_and_quotes_path():
    with tempfile.NamedTemporaryFile("w", suffix=".cap", delete=False) as f:
        f.write('# comment\nvideo/*; mpv "%s"\naudio/ogg; mpv --speed=1.3 "%s"; needsterminal\n'
                'image/*; \\\n  qview "%s"\n')
    entries = parse_mailcap(f.name)
    assert find_viewer(entries, "audio/ogg", "/tmp/a b.ogg") == "mpv --speed=1.3 '/tmp/a b.ogg'"
    assert find_viewer(entries, "video/mp4", "/x.mp4") == "mpv /x.mp4"
    assert find_viewer(entries, "image/png", "/x.png") == "qview /x.png"
    assert find_viewer(entries, "text/plain", "/x") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
