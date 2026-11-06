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
                "id": "c1", "function": {"name": "web_search", "arguments": {"query": "eris"}},
            }]}})]
        return [json.dumps({"message": {"content": self.answer}})]


async def _no_icon(domain):
    return None


async def _png_icon(domain):
    return b"\x89PNG", "image/png"


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

    def test_a_new_chat_is_named_from_what_was_asked(self):
        """The sidebar's placeholder is replaced by the model's own name.

        The name is a second model call made from the first message, and it
        reaches the browser as its own frame before the stream ends.
        """
        events = sse_events(self.h.ask("how do I validate an email", chat_id="c"))
        titled = [e for e in events if e.get("type") == "title"]
        self.assertEqual(len(titled), 1, [e.get("type") for e in events])
        self.assertEqual(titled[0]["chat_id"], "c")
        self.assertEqual(titled[0]["title"], "the answer")
        # The frame is the whole point: the sidebar has to be able to read the
        # name off the stream without going back for the chat.
        listed = self.h.client.get("/api/chats").json()["items"]
        self.assertEqual([c["title"] for c in listed], [titled[0]["title"]])

    def test_a_name_the_user_chose_survives_the_model_s_own(self):
        """The rename can land while the answer is still streaming."""
        chat_id = "c"
        sse_events(self.h.ask("how do I validate an email", chat_id=chat_id))
        self.h.client.patch(f"/api/chats/{chat_id}", json={"title": "Nama Saya"})
        sse_events(self.h.ask("lanjutkan", chat_id=chat_id))
        listed = self.h.client.get("/api/chats").json()["items"]
        self.assertEqual([c["title"] for c in listed], ["Nama Saya"])

    def test_an_ongoing_chat_is_not_renamed_by_a_later_turn(self):
        chat_id = "c"
        sse_events(self.h.ask("how do I validate an email", chat_id=chat_id))
        first = sse_events(self.h.ask("lanjutkan", chat_id=chat_id))
        self.assertEqual([e for e in first if e.get("type") == "title"], [])

    def test_the_naming_call_is_capped_and_asked_apart(self):
        """A name is a phrase, so it is a short call of its own."""
        self.h.stub.payloads.clear()
        sse_events(self.h.ask("how do I validate an email", chat_id="c"))
        naming = [p for p in self.h.stub.payloads
                  if p.get("messages", [{}])[0].get("content", "").startswith("Write the title")]
        self.assertEqual(len(naming), 1, [p.get("messages") for p in self.h.stub.payloads])
        self.assertEqual(naming[0]["options"]["num_predict"], 24)
        self.assertFalse(naming[0].get("tools"))

    def test_an_unnamed_chat_alone_is_never_named(self):
        """Nothing was asked, so there is nothing to name it from."""
        events = sse_events(self.h.client.post("/api/chat", json={
            "messages": [{"role": "user", "content": ""}], "chat_id": "c",
        }))
        self.assertEqual([e for e in events if e.get("type") == "title"], [])

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
        self.assertIn("Web search unavailable", json.dumps(fed_back))

    def test_a_model_that_keeps_asking_stops_at_the_round_budget(self):
        """The budget, not the model, decides when the tool rounds end."""
        self.h.stub.stop()

        class Greedy(StubModel):
            def reply(self, payload):
                if payload.get("tools"):
                    return [json.dumps({"message": {"content": "", "tool_calls": [{
                        "id": f"c{len(self.payloads)}",
                        "function": {"name": "web_search", "arguments": {"query": "eris"}},
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

        self.assertEqual(api.sse_frame(emitter.emit(ev.MODEL_DELTA, text="hi", round=2)),
                         {"type": "delta", "r": 2, "text": "hi"})
        self.assertEqual(api.sse_frame(emitter.emit(ev.MODEL_THINKING, text="hmm", round=2)),
                         {"type": "thinking", "r": 2, "text": "hmm"})
        # No round stated still has to give the browser one to key on.
        self.assertEqual(api.sse_frame(emitter.emit(ev.MODEL_THINKING, text="hmm")),
                         {"type": "thinking", "r": 0, "text": "hmm"})

    def test_a_tool_failure_reaches_the_browser(self):
        from app.runtime.events import Emitter
        from app.runtime import events as ev

        frame = api.sse_frame(Emitter().emit(
            ev.TOOL_FAILED, tool="web_search", code="timeout", message="too slow"))
        self.assertEqual(frame["type"], "tool_error")
        self.assertIn("timeout", frame["code"])

    def test_a_failed_fetch_carries_the_reason_the_card_replays(self):
        """After a reload the streamed frames are gone and only the stored row
        is left, so the reason has to travel on the frame that closes the read."""
        from app.runtime.events import Emitter
        from app.runtime import events as ev

        frame = api.sse_frame(Emitter().emit(
            ev.TOOL_FAILED, tool="fetch_url", round=1,
            code="tool_error", message="Could not read https://a.com: the server answered 403.",
            reason="the server answered 403"))
        self.assertEqual(frame["type"], "tool_error")
        self.assertEqual(frame["tool"], "fetch_url")
        self.assertEqual(frame["reason"], "the server answered 403")

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

    def test_a_weather_card_travels_whole_on_the_frame_that_ends_the_tool(self):
        """The card is drawn in one pass, so the payload it draws from rides on
        the closing frame; without it the browser has a tool call and no card."""
        from app.runtime.events import Emitter
        from app.runtime import events as ev

        card = {"place": "Bandung, Indonesia", "now": {"temp": 27.4},
                "forecast": [{"date": "2026-11-03", "min": 22.0, "max": 30.0}]}
        frame = api.sse_frame(Emitter().emit(
            ev.TOOL_COMPLETED, tool="weather", round=1, chars=200, sources=1, card=card))
        self.assertEqual(frame["type"], "weather")
        self.assertEqual(frame["r"], 1)
        self.assertEqual(frame["card"], card)

    def test_a_card_lands_after_the_prose_the_same_round_already_wrote(self):
        """A round can write a sentence and then call a tool.

        The browser appends the card where it arrives, so the frame has to come
        after that round's prose. Hoisting it above text the reader already
        passed is what made a card look like it never showed up.
        """
        from app.tools import weather as weather_mod

        class Preambling(StubModel):
            def reply(self, payload):
                rounds = sum(1 for p in self.payloads if p.get("tools"))
                if payload.get("tools") and rounds == 1:
                    return [
                        json.dumps({"message": {"content": "Let me check. "}}),
                        json.dumps({"message": {"content": "", "tool_calls": [{
                            "id": "c1",
                            "function": {"name": "weather",
                                         "arguments": {"location": "Bandung"}},
                        }]}}),
                    ]
                return [json.dumps({"message": {"content": "the answer"}})]

        card = {"place": "Bandung", "now": {"temp": 27.0}, "forecast": []}

        async def fake_read(args):
            return True, "Weather for Bandung", "", {"card": card}

        self.h.stub.stop()
        self.h.stub = Preambling()
        settings.model_base_url = self.h.stub.start()
        old = weather_mod.read
        weather_mod.read = fake_read
        try:
            events = sse_events(self.h.ask("cuaca bandung?"))
        finally:
            weather_mod.read = old

        shape = [(e["type"], e["r"]) for e in events
                 if e["type"] in ("delta", "weather")]
        self.assertEqual(shape, [("delta", 0), ("weather", 0), ("delta", 1)])
        weather = next(e for e in events if e["type"] == "weather")
        self.assertEqual(weather["card"], card, "the card frame carried no payload")

    def test_a_tool_with_no_card_sends_none(self):
        """`card` is absent on every other tool's closing frame, so the mapping
        keys off it rather than emitting an empty frame for the whole registry."""
        from app.runtime.events import Emitter
        from app.runtime import events as ev

        frame = api.sse_frame(Emitter().emit(
            ev.TOOL_COMPLETED, tool="read_file", round=0, chars=10, sources=0))
        self.assertIsNone(frame)

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

    def test_a_reopened_chat_sends_its_earlier_turns_back(self):
        """A chat reloaded from storage reaches the model with its history.

        This is the round trip the browser makes: the turn is stored, the chat
        is loaded back, and the next question carries what was loaded. A server
        that dropped the earlier turns here would answer as if the chat were
        new, however complete the page looked.
        """
        sse_events(self.h.ask("first question", chat_id="c"))
        history = self.h.client.get("/api/chats/c").json()["items"]
        self.h.stub.payloads.clear()
        replay = [{"role": t["role"], "content": t["content"]} for t in history]
        replay.append({"role": "user", "content": "second question"})
        sse_events(self.h.client.post("/api/chat", json={"messages": replay, "chat_id": "c"}))
        fed = self.h.stub.calls[-1]["messages"]
        self.assertEqual([m["role"] for m in fed],
                         ["system", "user", "assistant", "user"])
        self.assertIn("first question", fed[1]["content"])
        self.assertIn("second question", fed[3]["content"])

    def test_a_long_history_is_windowed_instead_of_sent_whole(self):
        """A chat that outgrew the budget keeps its newest turns, whole."""
        turns = []
        for i in range(40):
            turns.append({"role": "user", "content": f"q{i} " + "x" * 900})
            turns.append({"role": "assistant", "content": f"a{i} " + "y" * 900})
        turns.append({"role": "user", "content": "q40 " + "x" * 900})
        sse_events(self.h.client.post("/api/chat", json={"messages": turns}))
        fed = self.h.stub.calls[-1]["messages"]
        self.assertLess(len(fed), len(turns) + 1, "the whole history was sent")
        self.assertEqual(fed[1]["role"], "user", "the window does not open on a question")
        self.assertIn("q40", fed[-1]["content"])

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

    def test_a_missing_favicon_is_a_404_not_a_broken_row(self):
        """The row keeps its letter chip when the site has no icon, so the
        route answers 404 rather than an empty image the browser would try to
        paint."""
        from app import favicons

        old = favicons.icon_for
        favicons.icon_for = _no_icon
        try:
            r = self.h.client.get("/api/favicon?domain=no-icon.example")
        finally:
            favicons.icon_for = old
        self.assertEqual(r.status_code, 404)

        favicons.icon_for = _png_icon
        try:
            r = self.h.client.get("/api/favicon?domain=has-icon.example")
        finally:
            favicons.icon_for = old
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertEqual(r.content, b"\x89PNG")

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
        self.assertEqual(self.anon.get("/api/favicon?domain=example.com").status_code, 401)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)