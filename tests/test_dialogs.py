"""Runnable checks: python tests/test_dialogs.py

Settings is a dialog over the chat page, not a route: one shared shell in
dialog.css and dialog.js, one root in index.html, one sidebar opener, and a shim
where the old page used to be. These checks read the shipped files; there is no
browser here, so nothing about layout is asserted.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def check_the_dialog_is_in_the_chat_page():
    """One shell, one root, loaded by the chat page.

    The root list is derived from the markup rather than written down here: a
    new dialog is expected to arrive, and a test that has to be edited for each
    one stops being a check and becomes a chore.
    """
    chat = read("index.html")
    assert "/dialog.css" in chat, "the chat page does not load the dialog shell"
    assert '/settings.js" type="module"' in chat, "settings.js is not a module"

    roots = re.findall(r'id="(\w+Modal)" data-hash="(\w+)"', chat)
    assert roots, f"no dialog roots found: {roots}"
    for root, hashed in roots:
        assert f'id="{root}"' in chat, f"no {root} in the chat page"
        assert f'data-hash="{hashed}"' in chat, f"the {hashed} dialog has no hash"
    # Every root needs its own role, modality, and two dismissers.
    assert chat.count('role="dialog"') == len(roots), "each dialog needs its own role"
    assert chat.count('aria-modal="true"') == len(roots), "each dialog needs aria-modal"
    assert chat.count("data-close") == len(roots) * 2, "each dialog needs a scrim and a close"

    # A surface with several sections wears a rail that names each panel it
    # switches between, and every named panel has to be on the page.
    rails = re.findall(r'<nav class="dlg-nav" data-nav[^>]*>(.*?)</nav>', chat, re.S)
    assert rails, "no section rail"
    for rail in rails:
        sections = re.findall(r'data-sec="(\w+)"', rail)
        assert sections, "a rail with no sections"
        for sec in sections:
            assert f'id="sec-{sec}"' in chat, f"the rail names a missing panel: {sec}"


def check_the_opener_is_a_button():
    """No entry navigates any more."""
    chat = read("index.html")
    openers = re.findall(r'id="(side\w+)"[^>]*aria-controls="(\w+Modal)"', chat)
    assert openers, f"the sidebar opener was not found: {openers}"
    roots = dict(re.findall(r'id="(\w+Modal)" data-hash="(\w+)"', chat))
    for opener, controls in openers:
        assert f'id="{opener}"' in chat, f"no {opener} in the sidebar"
        assert controls in roots, f"{opener} opens a dialog that is not on the page: {controls}"
        assert 'aria-haspopup="dialog"' in chat, "an opener does not announce a dialog"
    assert "location.href = \"/settings.html\"" not in read("app.js"), "app.js still navigates to Settings"


def check_the_visible_panel_is_not_hidden():
    """The dialog opens onto content, not onto nothing.

    `select()` is what unhides a panel; a panel that ships `hidden` with no rail
    to call select would open the dialog empty.
    """
    chat = read("index.html")
    for sheet in re.findall(r'<div class="dlg-sheet[^"]*" id="(\w+Sheet)"', chat):
        block = chat.split(f'id="{sheet}"', 1)[1]
        panel = re.search(r'<section class="dlg-panel" id="sec-\w+"[^>]*>', block)
        assert panel, f"{sheet} has no panel"
        assert "hidden" not in panel.group(0), f"{sheet} opens onto a hidden panel"


def check_the_shell_lives_in_one_file():
    """dialog.css owns the shell; the surface owns only its content."""
    shell = read("dialog.css")
    for cls in (".dlg-modal", ".dlg-scrim", ".dlg-sheet", ".dlg-head", ".dlg-close",
                ".dlg-layout", ".dlg-nav", ".dlg-panel"):
        assert cls in shell, f"dialog.css is missing {cls}"
    surface = read("settings.css")
    assert ".dlg-modal" not in surface, "settings.css redefines the shell"
    assert ".dlg-nav button" not in surface, "settings.css restyles the rail"
    assert ".dlg-back" not in surface, "settings.css still styles a back control"


def check_the_old_page_only_redirects():
    """The route still resolves, and lands in the dialog."""
    page = read("settings.html")
    assert "/#settings" in page, "settings.html does not point at /#settings"
    assert 'id="sec-' not in page, "settings.html still renders its own panels"
    assert "<script src=" not in page, "settings.html still loads an app script"


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
