"""Runnable checks: python tests/test_media.py

Picture uploads have three slots. Avatar and cover write their own users
column; the model picture writes the account's prefs blob, because it belongs
with the model choice and the prompt rather than in the users table. These
checks pin where each one lands and confirm that replacing or removing a
picture deletes the file it replaced.
"""

import base64
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from fastapi.testclient import TestClient

from app import api, auth

# 1x1 transparent PNG. Small enough to inline, valid enough to pass the
# image/* guard and the 2MB cap.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


class Harness:
    def __init__(self):
        self.dir = tempfile.mkdtemp()
        os.environ["AUTH_DB"] = os.path.join(self.dir, "users.db")
        auth._db = None
        # Uploads must not land in the repo's static/uploads during a test run.
        self.old_media = api.MEDIA_DIR
        api.MEDIA_DIR = Path(self.dir) / "uploads"
        self.client = TestClient(api.app)

    def close(self):
        self.client.close()
        api.MEDIA_DIR = self.old_media
        if auth._db is not None:
            auth._db.close()
            auth._db = None
        os.environ.pop("AUTH_DB", None)

    def register(self):
        r = self.client.post(
            "/api/auth/register",
            json={"name": "Tester", "email": "t@example.com", "password": "secret1"},
        )
        assert r.status_code == 200, r.text

    def upload(self, kind):
        r = self.client.post(
            f"/api/auth/media?kind={kind}",
            files={"file": ("pic.png", PNG, "image/png")},
        )
        assert r.status_code == 200, r.text
        return r.json()["url"]

    def me(self):
        r = self.client.get("/api/auth/me")
        assert r.status_code == 200, r.text
        return r.json()

    def stored_files(self):
        d = api.MEDIA_DIR
        return sorted(p.name for p in d.iterdir()) if d.exists() else []


def check_model_picture_lands_in_prefs():
    """kind=model stores its URL in prefs, not in a users column."""
    h = Harness()
    try:
        h.register()
        url = h.upload("model")
        assert url.startswith("/uploads/"), url
        u = h.me()
        assert u["prefs"]["model_avatar"] == url, u["prefs"]
        assert not u["avatar"], u["avatar"]
    finally:
        h.close()


def check_avatar_keeps_its_column():
    """The older kinds still write their own column, so the model slot did not
    pull them into prefs."""
    h = Harness()
    try:
        h.register()
        url = h.upload("avatar")
        u = h.me()
        assert u["avatar"] == url, u
        assert not (u["prefs"] or {}).get("model_avatar"), u["prefs"]
    finally:
        h.close()


def check_replacing_model_picture_deletes_the_old_file():
    h = Harness()
    try:
        h.register()
        first = h.upload("model")
        second = h.upload("model")
        assert first != second, "the same name was reused"
        assert h.me()["prefs"]["model_avatar"] == second
        assert h.stored_files() == [second.split("/")[-1]], h.stored_files()
    finally:
        h.close()


def check_removing_model_picture_clears_prefs_and_file():
    h = Harness()
    try:
        h.register()
        h.upload("model")
        r = h.client.delete("/api/auth/media?kind=model")
        assert r.status_code == 200, r.text
        assert "model_avatar" not in h.me()["prefs"], h.me()["prefs"]
        assert h.stored_files() == [], h.stored_files()
    finally:
        h.close()


def check_unknown_kind_is_rejected():
    h = Harness()
    try:
        h.register()
        r = h.client.post(
            "/api/auth/media?kind=banner",
            files={"file": ("pic.png", PNG, "image/png")},
        )
        assert r.status_code == 400, r.text
    finally:
        h.close()


def check_non_image_is_rejected():
    h = Harness()
    try:
        h.register()
        r = h.client.post(
            "/api/auth/media?kind=model",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert r.status_code == 400, r.text
        assert "model_avatar" not in h.me()["prefs"], h.me()["prefs"]
    finally:
        h.close()


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
    sys.exit(1 if failures else 0)
