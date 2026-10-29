"""Runnable checks: python tests/test_api.py

The HTTP surface end to end: the chat stream still speaks the frames the
browser reads, health reports each dependency honestly, and every route
refuses an anonymous caller.
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parents[1]

from fastapi.testclient import TestClient

from app import api, auth, chats, tokens
from app.config import settings


class StubModel:
    def __init__(self, calls_per_round=0, answer="the answer"):
        self.calls_per_round = calls_per_round
        self.answer = answer
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

            def do_GET(self):
                body = json.dumps({"data": [{"id": "stub-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
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

    @property
    def calls(self):
        """The payloads that were a chat completion.

        Asking the endpoint which thinking levels it takes reaches the same
        route, and those probes are not calls the loop made, so they are kept
        out of the count the round-budget checks are about.
        """
        return [p for p in self.payloads if p.get("stream")]

    def reply(self, payload):
        rounds_with_tools = sum(1 for p in self.payloads if p.get("tools"))
        if payload.get("tools") and self.calls_per_round and rounds_with_tools == 1:
            return [json.dumps({"message": {"content": "", "tool_calls": [{
                "id": "c1", "function": {"name": "calc", "arguments": {"expression": "2+2"}},
            }]}})]
        return [json.dumps({"message": {"content": self.answer}})]


def sse_events(resp):
    out = []
    for line in resp.text.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if data == "[DONE]":
            continue
        try:
            out.append(json.loads(data))
        except ValueError:
            continue
    return out


class Harness:
    """A signed-in client against a stub model, on temp databases."""

    def __init__(self, stub=None, worker=False):
        self.dir = tempfile.mkdtemp()
        os.environ["AUTH_DB"] = os.path.join(self.dir, "users.db")
        os.environ["CHAT_DB"] = os.path.join(self.dir, "chats.db")
        os.environ["TOKENS_DB"] = os.path.join(self.dir, "tokens.db")
        for mod in (auth, chats, tokens):
            if mod._db is not None:
                mod._db.close()
            mod._db = None
        self.old_url = settings.model_base_url
        self.old_searx = settings.searxng_url
        self.old_media = api.MEDIA_DIR
        api.MEDIA_DIR = Path(self.dir) / "uploads"
        self.stub = stub or StubModel()
        settings.model_base_url = self.stub.start()
        settings.searxng_url = "http://127.0.0.1:1"
        self.client = TestClient(api.app)
        self.register(self.client, "one@example.com")

    @staticmethod
    def register(client, email):
        r = client.post("/api/auth/register",
                        json={"name": "Tester", "email": email, "password": "secret1"})
        assert r.status_code == 200, r.text

    def second(self, email="two@example.com"):
        c = TestClient(api.app)
        self.register(c, email)
        return c

    def close(self):
        self.client.close()
        settings.model_base_url = self.old_url
        settings.searxng_url = self.old_searx
        api.MEDIA_DIR = self.old_media
        self.stub.stop()
        for mod in (auth, chats, tokens):
            if mod._db is not None:
                mod._db.close()
                mod._db = None
        for name in ("AUTH_DB", "CHAT_DB", "TOKENS_DB"):
            os.environ.pop(name, None)

    def ask(self, message="hello", **extra):
        body = {"messages": [{"role": "user", "content": message}]}
        body.update(extra)
        return self.client.post("/api/chat", json=body)


class ChatStreamChecks(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_a_simple_answer_streams_as_deltas(self):
        events = sse_events(self.h.ask())
        deltas = "".join(e.get("text", "") for e in events if e.get("type") == "delta")
        self.assertEqual(deltas.strip(), "the answer")

    def test_the_stream_ends_with_done(self):
        r = self.h.ask()
        self.assertIn("[DONE]", r.text)

    def test_a_tool_round_runs_and_feeds_back(self):
        """A tool call is executed and its result goes back to the model."""
        self.h.stub.stop()
        self.h.stub = StubModel(calls_per_round=1)
        settings.model_base_url = self.h.stub.start()
        events = sse_events(self.h.ask())
        deltas = "".join(e.get("text", "") for e in events if e.get("type") == "delta")
        self.assertEqual(deltas.strip(), "the answer")
        # One round asked for the tool; the next answered, so two calls.
        self.assertEqual(len(self.h.stub.calls), 2,
                         [len(p["messages"]) for p in self.h.stub.calls])
        fed_back = self.h.stub.calls[1]["messages"]
        self.assertTrue(any(m.get("role") == "tool" for m in fed_back),
                        "the tool result never reached the model")
        self.assertIn("2+2 = 4", json.dumps(fed_back))

    def test_a_model_that_keeps_asking_stops_at_the_round_budget(self):
        """The budget, not the model, decides when the tool rounds end."""
        self.h.stub.stop()

        class Greedy(StubModel):
            def reply(self, payload):
                if payload.get("tools"):
                    return [json.dumps({"message": {"content": "", "tool_calls": [{
                        "id": f"c{len(self.payloads)}",
                        "function": {"name": "calc", "arguments": {"expression": "1+1"}},
                    }]}})]
                return [json.dumps({"message": {"content": "final"}})]

        self.h.stub = Greedy()
        settings.model_base_url = self.h.stub.start()
        events = sse_events(self.h.ask())
        # Three tool rounds, then one call with no tools to get an answer.
        self.assertEqual(len(self.h.stub.calls), settings.max_tool_rounds + 1)
        self.assertFalse(self.h.stub.calls[-1].get("tools"),
                         "the final call still offered tools")
        deltas = "".join(e.get("text", "") for e in events if e.get("type") == "delta")
        self.assertEqual(deltas.strip(), "final")

    def test_the_search_frame_is_what_the_ui_draws(self):
        """The one frame the frontend still switches on, mapped from the event."""
        from app.runtime.events import Emitter
        from app.runtime import events as ev

        emitter = Emitter()
        frame = api.sse_frame(
            emitter.emit(ev.TOOL_STARTED, tool="web_search", round=1, args={"query": "gold price"})
        )
        self.assertEqual(frame, {"type": "tool", "r": 1, "query": "gold price"})

        sources = api.sse_frame(emitter.emit(
            ev.SOURCE_FOUND, n=1, source="https://a.com", title="A", kind="web"))
        self.assertEqual(sources["type"], "sources")
        self.assertEqual(sources["items"][0]["source"], "https://a.com")

        self.assertEqual(api.sse_frame(emitter.emit(ev.MODEL_DELTA, text="hi")),
                         {"type": "delta", "text": "hi"})
        self.assertEqual(api.sse_frame(emitter.emit(ev.MODEL_THINKING, text="hmm")),
                         {"type": "thinking", "text": "hmm"})

    def test_a_tool_failure_reaches_the_browser(self):
        from app.runtime.events import Emitter
        from app.runtime import events as ev

        frame = api.sse_frame(Emitter().emit(
            ev.TOOL_FAILED, tool="web_search", code="timeout", message="too slow"))
        self.assertEqual(frame["type"], "tool_error")
        self.assertIn("timeout", frame["code"])

    def test_a_fetch_is_its_own_card_and_carries_its_round(self):
        """A read opened by the model, not by the search: two frames, and the
        round on both so the browser can close the right card."""
        from app.runtime.events import Emitter
        from app.runtime import events as ev

        e = Emitter()
        opened = api.sse_frame(e.emit(
            ev.TOOL_STARTED, tool="fetch_url", round=2, args={"url": "https://a.com/x"}))
        self.assertEqual(opened, {"type": "fetch", "r": 2, "url": "https://a.com/x"})

        closed = api.sse_frame(e.emit(ev.TOOL_COMPLETED, tool="fetch_url", round=2, chars=1234))
        self.assertEqual(closed, {"type": "fetch_done", "r": 2, "ok": True, "chars": 1234})

    def test_a_source_states_its_round_and_whether_it_was_read(self):
        """The card files a result under the round that found it, and marks the
        few whose page text was actually pulled."""
        from app.runtime.events import Emitter
        from app.runtime import events as ev

        frame = api.sse_frame(Emitter().emit(
            ev.SOURCE_FOUND, round=1, n=1, source="https://a.com", title="A",
            kind="web", read=True))
        self.assertEqual(frame["r"], 1)
        self.assertTrue(frame["items"][0]["read"])

        # A source that reached the list on a snippet alone says so.
        plain = api.sse_frame(Emitter().emit(
            ev.SOURCE_FOUND, round=1, n=2, source="https://b.com", kind="web"))
        self.assertFalse(plain["items"][0]["read"])

    def test_the_turn_is_persisted_and_reloads(self):
        chat_id = "chat-1"
        sse_events(self.h.ask(chat_id=chat_id))
        turns = self.h.client.get(f"/api/chats/{chat_id}").json()["items"]
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])
        self.assertEqual(turns[1]["content"].strip(), "the answer")

    def test_a_turn_cannot_be_written_into_another_accounts_chat(self):
        other = self.h.second()
        sse_events(other.post("/api/chat", json={
            "messages": [{"role": "user", "content": "theirs"}],
            "chat_id": "shared",
        }))
        sse_events(self.h.ask("mine", chat_id="shared"))
        theirs = other.get("/api/chats/shared").json()["items"]
        self.assertEqual([t["content"] for t in theirs if t["role"] == "user"], ["theirs"])

    def test_regenerating_replaces_the_previous_answer(self):
        chat_id = "c"
        sse_events(self.h.ask("q", chat_id=chat_id))
        sse_events(self.h.ask("q", chat_id=chat_id, regenerate=True))
        turns = self.h.client.get(f"/api/chats/{chat_id}").json()["items"]
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])

    def test_a_model_that_is_down_reports_an_error_frame(self):
        self.h.stub.stop()
        settings.model_base_url = "http://127.0.0.1:1/v1"
        events = sse_events(self.h.ask())
        self.assertTrue(any(e.get("type") == "error" for e in events))

    def test_an_anonymous_caller_cannot_chat(self):
        anon = TestClient(api.app)
        r = anon.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r.status_code, 401)

    def test_health_reports_each_dependency(self):
        got = self.h.client.get("/api/health").json()
        for key in ("model", "search"):
            self.assertIn(key, got)
        self.assertEqual(got["model"], "up")

    def test_models_are_listed_with_the_default(self):
        got = self.h.client.get("/api/models").json()
        self.assertIn("stub-model", got["items"])
        self.assertTrue(got["default"])

    def test_the_stats_readout_says_nothing_it_was_not_told(self):
        """The stub serves neither `/api/ps` nor `/api/show`, so every figure
        the readout would derive is absent rather than invented."""
        got = self.h.client.get("/api/stats").json()
        for key in ("model", "loaded", "context", "kv", "gpu", "last"):
            self.assertIn(key, got)
        self.assertFalse(got["loaded"])
        self.assertIsNone(got["kv"]["bytes"])
        self.assertIsNone(got["last"])


class AnonymousChecks(unittest.TestCase):
    """Every route that touches user state refuses an anonymous caller."""

    def setUp(self):
        self.h = Harness()
        self.anon = TestClient(api.app)

    def tearDown(self):
        self.anon.close()
        self.h.close()

    def test_reads_need_a_session(self):
        for path in ("/api/chats", "/api/auth/me", "/api/models", "/api/stats"):
            self.assertEqual(self.anon.get(path).status_code, 401, path)

    def test_writes_need_a_session(self):
        self.assertEqual(self.anon.post("/api/prefs", json={}).status_code, 401)
        self.assertEqual(self.anon.patch("/api/chats/x", json={"title": "t"}).status_code, 401)


class TokenChecks(unittest.TestCase):
    """API tokens. The point of the feature is what a token may and may not do,
    so the checks are about the scope line as much as about the store."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def bearer(self, value):
        c = TestClient(api.app)
        c.headers.update({"Authorization": f"Bearer {value}"})
        return c

    def mint(self, name="laptop"):
        r = self.h.client.post("/api/tokens", json={"name": name})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_token_is_shown_once_and_never_again(self):
        got = self.mint()
        self.assertTrue(got["token"].startswith("eris_"))
        listed = self.h.client.get("/api/tokens").json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token", listed[0])
        self.assertEqual(listed[0]["name"], "laptop")

    def test_a_token_authenticates_a_read(self):
        value = self.mint()["token"]
        c = self.bearer(value)
        self.assertEqual(c.get("/api/auth/me").status_code, 200)
        self.assertEqual(c.get("/api/models").status_code, 200)
        c.close()

    def test_a_token_is_refused_by_everything_that_writes(self):
        """The scope line: a leaked token cannot be escalated into a takeover."""
        value = self.mint()["token"]
        c = self.bearer(value)
        self.assertEqual(c.post("/api/prefs", json={"model": "m"}).status_code, 401)
        self.assertEqual(c.post("/api/auth/profile", json={"name": "x"}).status_code, 401)
        self.assertEqual(c.post("/api/auth/password",
                                json={"old": "secret1", "new": "secret2"}).status_code, 401)
        self.assertEqual(c.post("/api/tokens", json={"name": "another"}).status_code, 401)
        self.assertEqual(c.get("/api/tokens").status_code, 401)
        self.assertEqual(c.patch("/api/chats/x", json={"title": "t"}).status_code, 401)
        c.close()

    def test_a_revoked_token_stops_working(self):
        got = self.mint()
        self.assertEqual(self.h.client.delete(f"/api/tokens/{got['id']}").status_code, 200)
        c = self.bearer(got["token"])
        self.assertEqual(c.get("/api/auth/me").status_code, 401)
        c.close()

    def test_a_guess_that_was_never_minted_is_401(self):
        c = self.bearer("eris_not-a-real-token")
        self.assertEqual(c.get("/api/auth/me").status_code, 401)
        c.close()

    def test_one_account_cannot_revoke_anothers_token(self):
        theirs = self.h.second()
        got = theirs.post("/api/tokens", json={"name": "theirs"}).json()
        self.assertEqual(self.h.client.delete(f"/api/tokens/{got['id']}").status_code, 404)
        self.assertEqual(self.h.client.get("/api/tokens").json()["items"], [])

    def test_resolving_a_token_stamps_the_last_use(self):
        got = self.mint()
        c = self.bearer(got["token"])
        c.get("/api/auth/me")
        c.close()
        listed = self.h.client.get("/api/tokens").json()["items"][0]
        self.assertGreater(listed["used"], 0)


class McpChecks(unittest.TestCase):
    """The MCP endpoint. The tools it exposes are the registered ones, so what is
    checked is the protocol shape and the scope the token arrives with."""

    HEADERS = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}

    def setUp(self):
        self.h = Harness()
        self.value = self.h.client.post("/api/tokens", json={"name": "mcp"}).json()["token"]
        self.client = TestClient(api.app)
        self.client.headers.update({"Authorization": f"Bearer {self.value}"})

    def tearDown(self):
        self.client.close()
        self.h.close()

    def rpc(self, method, params=None, rpc_id=1, headers=None, **kw):
        body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            body["params"] = params
        return self.client.post("/mcp", json=body, headers=headers or self.HEADERS, **kw)

    def test_initialize_names_the_protocol_and_issues_a_session(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18",
                                    "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(r.status_code, 200, r.text)
        result = r.json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "eris")
        self.assertTrue(r.headers.get("mcp-session-id"))

    def test_tools_list_returns_the_registered_schemas(self):
        r = self.rpc("tools/list", {})
        tools = r.json()["result"]["tools"]
        names = sorted(t["name"] for t in tools)
        self.assertEqual(names, ["calc", "fetch_url", "web_search"])
        for t in tools:
            self.assertIn("inputSchema", t)
        calc = next(t for t in tools if t["name"] == "calc")
        self.assertTrue(calc["annotations"]["readOnlyHint"])

    def test_a_read_only_tool_runs_through_mcp(self):
        r = self.rpc("tools/call", {"name": "calc", "arguments": {"expression": "2+2"}})
        self.assertEqual(r.status_code, 200, r.text)
        result = r.json()["result"]
        self.assertFalse(result["isError"])
        self.assertIn("4", result["content"][0]["text"])

    def test_an_unknown_tool_is_a_protocol_error(self):
        """The request was malformed as a request — this is not a tool saying no."""
        r = self.rpc("tools/call", {"name": "nope", "arguments": {}})
        err = r.json()["error"]
        self.assertEqual(err["code"], -32602)
        self.assertIn("nope", err["message"])

    def test_an_unknown_method_is_a_protocol_error(self):
        self.assertEqual(self.rpc("tools/nope").json()["error"]["code"], -32601)

    def test_a_notification_is_accepted_with_no_body(self):
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                             headers=self.HEADERS)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.content, b"")

    def test_a_bad_accept_header_is_refused(self):
        r = self.rpc("tools/list", {}, headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 406)

    def test_a_bad_version_is_refused(self):
        r = self.rpc("tools/list", {}, headers={**self.HEADERS, "MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(r.status_code, 400)

    def test_an_unknown_session_is_404(self):
        r = self.rpc("tools/list", {}, headers={**self.HEADERS, "Mcp-Session-Id": "nope"})
        self.assertEqual(r.status_code, 404)

    def test_delete_terminates_the_session(self):
        sid = self.rpc("initialize", {"protocolVersion": "2025-06-18"}).headers["mcp-session-id"]
        r = self.client.delete("/mcp", headers={**self.HEADERS, "Mcp-Session-Id": sid})
        self.assertEqual(r.status_code, 204)
        again = self.rpc("tools/list", {}, headers={**self.HEADERS, "Mcp-Session-Id": sid})
        self.assertEqual(again.status_code, 404)

    def test_get_is_405_because_this_server_never_pushes(self):
        self.assertEqual(self.client.get("/mcp", headers=self.HEADERS).status_code, 405)

    def test_a_cross_origin_request_is_refused(self):
        r = self.rpc("tools/list", {}, headers={**self.HEADERS, "Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_an_anonymous_caller_cannot_speak_mcp(self):
        anon = TestClient(api.app)
        r = anon.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      headers=self.HEADERS)
        self.assertEqual(r.status_code, 401)
        anon.close()

    def test_a_gated_tool_refuses_rather_than_running(self):
        """An approval is a decision a token cannot make, so the call comes back
        as a tool error the client can read rather than as a silent run."""
        from app.tools.registry import Tool, ToolContext, registry
        from app.security import permissions

        called = []

        async def handler(args, ctx):
            called.append(args)
            return "ran"

        tool = Tool(
            name="write_thing", description="Changes something.", parameters={"type": "object"},
            handler=handler, permission=permissions.WRITE, tags=("chat",),
        )
        registry.add(tool)
        try:
            r = self.rpc("tools/call", {"name": "write_thing", "arguments": {}})
            result = r.json()["result"]
            self.assertTrue(result["isError"])
            self.assertEqual(called, [], "an unapproved tool ran")
        finally:
            registry.remove("write_thing")


if __name__ == "__main__":
    unittest.main(verbosity=2)