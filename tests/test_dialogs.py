"""Runnable checks: python tests/test_dialogs.py

The Library and Settings surfaces are dialogs over the chat page, not routes:
one shared shell in dialog.css and dialog.js, two roots in index.html, two
sidebar openers, and two shims where the old pages used to be. These checks read
the shipped files; there is no browser here, so nothing about layout is asserted.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def check_both_dialogs_are_in_the_chat_page():
    """One shell, two roots, both loaded by the chat page."""
    chat = read("index.html")
    assert "/dialog.css" in chat, "the chat page does not load the dialog shell"
    assert "/library.js\" type=\"module\"" in chat, "library.js is not a module"
    assert "/settings.js\" type=\"module\"" in chat, "settings.js is not a module"

    for root, hashed in (("libModal", "library"), ("setModal", "settings")):
        assert f'id="{root}"' in chat, f"no {root} in the chat page"
    assert chat.count('role="dialog"') == 2, "each dialog needs its own role"
    assert chat.count('aria-modal="true"') == 2, "each dialog needs aria-modal"
    assert 'data-hash="library"' in chat and 'data-hash="settings"' in chat, "a dialog has no hash"
    assert chat.count("data-nav") == 2, "each dialog needs a section rail"
    assert chat.count("data-close") == 4, "each dialog needs a scrim and a close button"


def check_the_sidebar_openers_are_dialog_buttons():
    """Neither entry navigates any more."""
    chat = read("index.html")
    for opener, controls in (("sideLibrary", "libModal"), ("sideProfile", "setModal")):
        assert f'id="{opener}"' in chat, f"no {opener} in the sidebar"
        assert f'aria-controls="{controls}"' in chat, f"{opener} does not name its dialog"
        assert f'aria-haspopup="dialog"' in chat, "an opener does not announce a dialog"
    assert "location.href = \"/settings.html\"" not in read("app.js"), "app.js still navigates to Settings"
    assert "location.href = \"/library.html\"" not in read("app.js"), "app.js still navigates to the Library"


def check_the_shell_lives_in_one_file():
    """dialog.css owns the shell; the surfaces own only their content."""
    shell = read("dialog.css")
    for cls in (".dlg-modal", ".dlg-scrim", ".dlg-sheet", ".dlg-head", ".dlg-close",
                ".dlg-layout", ".dlg-nav", ".dlg-panel"):
        assert cls in shell, f"dialog.css is missing {cls}"
    for name in ("library.css", "settings.css"):
        surface = read(name)
        assert ".dlg-modal" not in surface, f"{name} redefines the shell"
        assert ".dlg-nav button" not in surface, f"{name} restyles the rail"
        assert ".dlg-back" not in surface, f"{name} still styles a back control"


def check_the_old_pages_only_redirect():
    """The two routes still resolve, and land in the matching dialog."""
    for name, target in (("library.html", "/#library"), ("settings.html", "/#settings")):
        page = read(name)
        assert target in page, f"{name} does not point at {target}"
        assert 'id="sec-' not in page, f"{name} still renders its own panels"
        assert "<script src=" not in page, f"{name} still loads an app script"


def check_settings_keeps_its_form_styling():
    """The panel content survives the move, scoped to the dialog root."""
    css = read("settings.css")
    assert ".set-modal input" in css, "the settings fields lost their styling"
    assert ".set-modal textarea" in css, "the prompt textarea lost its styling"
    js = read("settings.js")
    assert 'from "/dialog.js"' in js, "settings.js does not use the shared shell"
    assert "eris:settings-saved" in js, "saved settings never tell the chat page"
    assert "eris:settings-saved" in read("app.js"), "the chat page never listens for saved settings"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("check_"):
            continue
        try:
            fn()
            print(f"ok  {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failures += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
