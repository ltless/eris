"""Runnable checks: python tests/test_tool_loop.py

The model keeps calling web_search until the round budget runs out. These
checks pin what the server is supposed to send the browser in that case.

A stub model server stands in for llama-server. It returns a tool call for as
long as the request offers tools, and plain text when the request offers none,
which is exactly the shape the real server has.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, ".")

from fastapi.testclient import TestClient

from app import api, auth, chats
from app.config import settings


class StubModel:
    """llama-server's /api/chat, reduced to the two replies that matter."""

    def __init__(self, calls_per_round=1, answer="final answer", repeat_first=False):
        self.calls_per_round = calls_per_round
        self.answer = answer
        self.repeat_first = repeat_first
        self.payloads = []
        self._srv = None
        self._thread = None

    def start(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                payload = json.loads(raw or b"{}")
                stub.payloads.append(payload)
                body = b"".join(line.encode() + b"\n" for line in stub.reply(payload))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._srv.server_address[1]}/v1"

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5)

    def reply(self, payload):
        rounds_so_far = sum(1 for p in self.payloads if p.get("tools"))
        out = [json.dumps({"message": {"thinking": "weighing search results"}})]
        if not payload.get("tools") or self.calls_per_round == 0:
            out.append(json.dumps({"message": {"content": self.answer}}))
            out.append(json.dumps({"message": {"content": ""}, "done": True}))
            return out
        calls = [
            {
                "id": f"call_{rounds_so_far}_{i}",
                "function": {"name": "web_search", "arguments": {"query": f"query {rounds_so_far}.{i}"}},
            }
            for i in range(self.calls_per_round)
        ]
        # One tool call per line, the way the real server emits them.
        for call in calls:
            out.append(json.dumps({"message": {"content": "", "tool_calls": [call]}}))
            if self.repeat_first and call is calls[0]:
                out.append(json.dumps({"message": {"content": "", "tool_calls": [call]}}))
        out.append(json.dumps({"message": {"content": ""}, "done": True}))
        return out


def sse_events(resp):
    events = []
    for line in resp.text.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if data == "[DONE]":
            continue
        events.append(json.loads(data))
    return events


class Harness:
    def __init__(self, stub):
        self.stub = stub
        self.dir = tempfile.mkdtemp()
        os.environ["AUTH_DB"] = os.path.join(self.dir, "users.db")
        os.environ["CHAT_DB"] = os.path.join(self.dir, "chats.db")
        auth._db = None
        chats._db = None
        self.old_url = settings.model_base_url
        self.old_searx = settings.searxng_url
        settings.model_base_url = stub.start()
        settings.searxng_url = "http://127.0.0.1:1"

    def close(self):
        settings.model_base_url = self.old_url
        settings.searxng_url = self.old_searx
        self.stub.stop()
        for mod in (auth, chats):
            if mod._db is not None:
                mod._db.close()
                mod._db = None
        for name in ("AUTH_DB", "CHAT_DB"):
            os.environ.pop(name, None)

    def ask(self, message="weather in Bali today"):
        with TestClient(api.app) as c:
            c.post(
                "/api/auth/register",
                json={"name": "Tester", "email": "t@example.com", "password": "secret1"},
            )
            r = c.post(
                "/api/chat",
                json={"messages": [{"role": "user", "content": message}]},
            )
            assert r.status_code == 200, r.text
            return sse_events(r)


def check_answer_after_budget():
    """The turn must end with an answer, not with the last search row."""
    stub = StubModel()
    h = Harness(stub)
    try:
        events = h.ask()
        rounds = {e["r"] for e in events if e.get("type") == "tool"}
        text = "".join(e.get("text", "") for e in events if e.get("type") == "delta")
        assert len(rounds) == settings.max_tool_rounds, f"rounds={sorted(rounds)} budget={settings.max_tool_rounds}"
        assert text.strip(), f"stream ended with no answer; events={[e.get('type') for e in events]}"
        assert text.strip() == stub.answer, text
    finally:
        h.close()


def check_answer_request_has_no_tools():
    """The final call must drop the tool list, or the model just searches again."""
    stub = StubModel()
    h = Harness(stub)
    try:
        h.ask()
        assert len(stub.payloads) == settings.max_tool_rounds + 1, len(stub.payloads)
        assert "tools" in stub.payloads[0], "first call should offer tools"
        assert not stub.payloads[-1].get("tools"), "last call should not offer tools"
    finally:
        h.close()


def check_stops_when_model_answers_first():
    """A model that answers immediately must not get a second call."""
    stub = StubModel(calls_per_round=0)
    h = Harness(stub)
    try:
        events = h.ask()
        text = "".join(e.get("text", "") for e in events if e.get("type") == "delta")
        assert text.strip() == stub.answer, text
        assert len(stub.payloads) == 1, len(stub.payloads)
    finally:
        h.close()


def check_two_calls_in_one_round():
    """Two tool calls arriving on separate stream lines must both run."""
    stub = StubModel(calls_per_round=2)
    h = Harness(stub)
    try:
        events = h.ask()
        first = [e for e in events if e.get("type") == "tool" and e["r"] == 0]
        assert len(first) == 2, f"round 0 ran {len(first)} of 2 calls"
    finally:
        h.close()


def check_repeated_call_id_runs_once():
    """A server that re-sends a call as its arguments grow must not run it twice."""
    stub = StubModel(repeat_first=True)
    h = Harness(stub)
    try:
        events = h.ask()
        first = [e for e in events if e.get("type") == "tool" and e["r"] == 0]
        assert len(first) == 1, f"round 0 ran {len(first)} calls for one id"
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