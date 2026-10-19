"""Smoke test against a live server.

    .venv\\Scripts\\python.exe tests/smoke_library.py --email you@example.com --password ...

Needs ChromaDB up and the embedding endpoint reachable, because this path
really embeds. It logs in, uploads a small text file, lists it, opens it,
checks the page is served, uploads the same filename again to confirm the first
copy is replaced, deletes the document, and confirms it is gone. Legacy rows
already in the store are reported, not asserted: they depend on what was
indexed before document ids existed.
"""

import argparse
import sys

import httpx

NAME = "smoke notes.txt"


def die(step, message):
    print(f"FAIL {step}: {message}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9000")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()

    with httpx.Client(base_url=args.base, timeout=60, follow_redirects=True) as c:
        r = c.post("/api/auth/login", json={"email": args.email, "password": args.password})
        if r.status_code != 200:
            die("log in", f"{r.status_code} {r.text}")
        print("ok  log in")

        text = "smoke test line\n" * 40
        r = c.post(
            "/api/documents/upload",
            files={"file": (NAME, text.encode("utf-8"), "text/plain")},
        )
        if r.status_code != 200:
            die("upload", f"{r.status_code} {r.text}")
        doc = r.json()
        if not doc.get("id"):
            die("upload", f"no id came back: {doc}")
        print(f"ok  upload ({doc['name']}, {doc['chunks']} chunks)")

        r = c.get("/api/documents")
        if r.status_code != 200:
            die("list", f"{r.status_code} {r.text}")
        items = r.json()["items"]
        if doc["id"] not in [i["id"] for i in items]:
            die("list", "the uploaded id is not in the list")
        legacy = [i for i in items if i["legacy"]]
        print(f"ok  list ({len(items)} rows, {len(legacy)} legacy)")

        r = c.get(f"/api/documents/{doc['id']}")
        if r.status_code != 200:
            die("inspect", f"{r.status_code} {r.text}")
        body = r.json()
        if not body["passages"] or body["passages"][0]["n"] != 0:
            die("inspect", f"no first passage came back: {body}")
        print(f"ok  inspect ({len(body['passages'])} of {body['chunks']} chunks)")

        r = c.get("/")
        if r.status_code != 200 or 'id="libModal"' not in r.text:
            die("page", f"{r.status_code} {r.text[:200]}")
        print("ok  page")

        # Uploading the same filename again replaces it: one row, one copy of
        # the chunks, and the response says what was retired.
        r = c.post(
            "/api/documents/upload",
            files={"file": (NAME, text.encode("utf-8"), "text/plain")},
        )
        if r.status_code != 200:
            die("re-upload", f"{r.status_code} {r.text}")
        again = r.json()
        if again.get("replaced") != 1:
            die("re-upload", f"expected 1 replaced document, got {again.get('replaced')}")
        rows = c.get("/api/documents").json()["items"]
        same = [i for i in rows if i["name"] == NAME]
        if [i["id"] for i in same] != [again["id"]]:
            die("re-upload", f"the previous copy is still listed: {same}")
        doc = again
        print(f"ok  re-upload (replaced 1, {doc['chunks']} chunks)")

        r = c.delete(f"/api/documents/{doc['id']}")
        if r.status_code != 200:
            die("delete", f"{r.status_code} {r.text}")
        print("ok  delete")

        r = c.get("/api/documents")
        items = r.json()["items"]
        if doc["id"] in [i["id"] for i in items]:
            die("verify", "the document is still listed after delete")
        legacy = [i for i in items if i["legacy"]]
        print("ok  verify")

    if legacy:
        print(f"note  {len(legacy)} pre-migration rows are in the store, left alone")
    print("smoke passed")


if __name__ == "__main__":
    main()
