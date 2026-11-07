"""Runnable checks: python tests/test_runtime.py

The shared runtime: the tool contract, the permission gate, budget enforcement,
event shape, and the loop itself against a stub model server. The loop is what
chat and research both run on, so these checks pin the behaviour the
whole app depends on.
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

from app.config import settings
from app.runtime import errors, events as ev
from app.runtime.budgets import Budget, BudgetExceeded, clip
from app.runtime.engine import Engine, RunRequest
from app.runtime.events import Emitter, Event, scrub
from app.security import permissions as perms
from app.tools import builtin
from app.tools.registry import Registry, Tool, ToolContext, ToolResult, registry


# --- the tool contract ---


class RegistryChecks(unittest.TestCase):
    def setUp(self):
        builtin.register_builtins()

    def test_registry_is_idempotent(self):
        builtin.register_builtins()
        self.assertEqual(sorted(registry.names()),
                         ["crypto", "crypto_trend", "equity", "equity_trend",
                          "fetch_url", "plan", "read_file", "screen", "wayback",
                          "weather", "web_search"])

    def test_specs_carry_name_description_and_schema(self):
        spec = next(s for s in registry.specs(("chat",))
                    if s["function"]["name"] == "web_search")
        self.assertEqual(spec["type"], "function")
        self.assertTrue(spec["function"]["description"])
        self.assertEqual(spec["function"]["parameters"]["required"], ["query"])

    def test_invalid_arguments_are_refused_before_running(self):
        r = asyncio.run(registry.call("web_search", {"query": 12}))
        self.assertFalse(r.success)
        self.assertEqual(r.error["code"], errors.INVALID_ARGUMENTS)

    def test_unknown_tool_is_named(self):
        r = asyncio.run(registry.call("nope", {}))
        self.assertFalse(r.success)
        self.assertEqual(r.error["code"], errors.UNKNOWN_TOOL)

    def test_duplicate_registration_raises(self):
        reg = Registry()
        tool = Tool(name="x", description="d", parameters={"type": "object"},
                    handler=lambda a: "ok")
        reg.add(tool)
        with self.assertRaises(ValueError):
            reg.add(tool)

    def test_a_handler_may_return_text_or_a_result(self):
        reg = Registry()

        async def text_handler(args):
            return "plain"

        async def result_handler(args):
            return ToolResult.ok("rich", n=2)

        reg.add(Tool(name="t", description="d", parameters={"type": "object"},
                     handler=text_handler))
        reg.add(Tool(name="r", description="d", parameters={"type": "object"},
                     handler=result_handler))
        self.assertEqual(asyncio.run(reg.call("t", {})).text, "plain")
        got = asyncio.run(reg.call("r", {}))
        self.assertEqual(got.text, "rich")
        self.assertEqual(got.data, {"n": 2})

    def test_a_raising_handler_becomes_a_failed_result(self):
        reg = Registry()

        def boom(args):
            raise RuntimeError("kaboom")

        reg.add(Tool(name="b", description="d", parameters={"type": "object"}, handler=boom))
        got = asyncio.run(reg.call("b", {}))
        self.assertFalse(got.success)
        self.assertIn("kaboom", got.error["message"])
        self.assertTrue(got.error["retryable"])

    def test_a_slow_handler_times_out(self):
        reg = Registry()

        async def slow(args):
            await asyncio.sleep(5)

        reg.add(Tool(name="s", description="d", parameters={"type": "object"},
                     handler=slow, timeout=0.05))
        got = asyncio.run(reg.call("s", {}))
        self.assertFalse(got.success)
        self.assertEqual(got.error["code"], "timeout")

    def test_a_sync_handler_is_supported(self):
        reg = Registry()
        reg.add(Tool(name="sync", description="d", parameters={"type": "object"},
                     handler=lambda args: f"got {args.get('x')}"))
        self.assertEqual(asyncio.run(reg.call("sync", {"x": 1})).text, "got 1")

    def test_handlers_receive_the_context(self):
        reg = Registry()

        async def needs_ctx(args, ctx):
            return f"user={ctx.user_id}"

        reg.add(Tool(name="c", description="d", parameters={"type": "object"},
                     handler=needs_ctx))
        got = asyncio.run(reg.call("c", {}, ToolContext(user_id=7)))
        self.assertEqual(got.text, "user=7")


# --- permissions ---


class PolicyChecks(unittest.TestCase):
    def test_read_only_runs_without_a_project(self):
        tool = Tool(name="r", description="d", parameters={}, handler=lambda a: "",
                    permission=perms.READ_ONLY)
        self.assertTrue(perms.authorize(tool, ToolContext()).allowed)

    def test_a_write_is_refused_for_want_of_a_way_to_ask(self):
        tool = Tool(name="w", description="d", parameters={}, handler=lambda a: "",
                    permission=perms.WRITE)
        decision = perms.authorize(tool, ToolContext(), {"proposal_id": "abc"})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "denied")

    def test_an_unknown_permission_is_refused(self):
        tool = Tool(name="x", description="d", parameters={}, handler=lambda a: "",
                    permission="god_mode")
        decision = perms.authorize(tool, ToolContext())
        self.assertFalse(decision.allowed)
        self.assertIn("unknown permission", decision.reason)

    def test_specs_offer_what_can_be_asked_for_and_withhold_the_rest(self):
        reg = Registry()
        reg.add(Tool(name="read", description="d", parameters={"type": "object"},
                     handler=lambda a: "", permission=perms.READ_ONLY, tags=("chat",)))
        reg.add(Tool(name="write", description="d", parameters={"type": "object"},
                     handler=lambda a: "", permission=perms.WRITE, tags=("chat",)))

        # Only the read is offered: a write would come back refused, so putting
        # it in front of the model spends a round on a schema it cannot use.
        names = {s["function"]["name"] for s in reg.specs_for(ToolContext())}
        self.assertEqual(names, {"read"})

    def test_a_tool_that_needs_something_from_the_run_is_only_offered_with_it(self):
        """`read_file` on a run with no files is a schema the model cannot use,
        offered on every round; the tool says when it has anything to offer."""
        reg = Registry()
        reg.add(Tool(name="read_file", description="d", parameters={"type": "object"},
                     handler=lambda a, c: "", permission=perms.READ_ONLY, tags=("chat",),
                     when=lambda ctx: bool(getattr(ctx, "files", ()))))
        self.assertEqual(reg.specs_for(ToolContext()), [])
        offered = reg.specs_for(ToolContext(files=({"name": "a", "text": "b"},)))
        self.assertEqual([s["function"]["name"] for s in offered], ["read_file"])

    def test_the_builtin_file_reader_is_offered_exactly_when_a_file_is(self):
        builtin.register_builtins()
        self.assertNotIn("read_file",
                         {s["function"]["name"] for s in registry.specs_for(ToolContext())})
        with_file = registry.specs_for(ToolContext(files=({"name": "a", "text": "b"},)))
        self.assertIn("read_file", {s["function"]["name"] for s in with_file})


# --- budgets ---


class BudgetChecks(unittest.TestCase):
    def test_model_calls_are_capped(self):
        b = Budget(model_calls=2)
        b.spend_model()
        b.spend_model()
        with self.assertRaises(BudgetExceeded) as ctx:
            b.spend_model()
        self.assertEqual(ctx.exception.kind, "model_calls")

    def test_tool_calls_are_capped(self):
        b = Budget(tool_calls=1)
        b.spend_tool()
        with self.assertRaises(BudgetExceeded) as ctx:
            b.spend_tool()
        self.assertEqual(ctx.exception.kind, "tool_calls")

    def test_elapsed_time_is_capped(self):
        b = Budget(seconds=0.0)
        with self.assertRaises(BudgetExceeded) as ctx:
            b.spend_model()
        self.assertEqual(ctx.exception.kind, "seconds")

    def test_retries_are_bounded(self):
        b = Budget(retries=1)
        self.assertTrue(b.allow_retry())
        self.assertFalse(b.allow_retry())

    def test_the_report_says_what_was_spent(self):
        b = Budget(model_calls=5, tool_calls=5)
        b.spend_model()
        b.spend_tool()
        used = b.used()
        self.assertEqual(used["model_calls"], 1)
        self.assertEqual(used["tool_calls"], 1)
        self.assertIn("seconds", used)

    def test_clip_bounds_long_text(self):
        self.assertEqual(clip("abc", 10), "abc")
        out = clip("x" * 100, 10)
        self.assertTrue(out.startswith("x" * 10))
        self.assertIn("90 characters omitted", out)


# --- events ---


class EventChecks(unittest.TestCase):
    def test_sequence_numbers_increase_from_one(self):
        e = Emitter()
        a = e.emit(ev.MODEL_DELTA, text="a")
        b = e.emit(ev.MODEL_DELTA, text="b")
        self.assertEqual((a.seq, b.seq), (1, 2))

    def test_an_emitter_can_resume_a_sequence(self):
        e = Emitter(start=10)
        self.assertEqual(e.emit(ev.MODEL_DELTA).seq, 11)

    def test_secrets_are_scrubbed_from_payloads(self):
        e = Emitter()
        out = e.emit(ev.TOOL_STARTED, args={"api_key": "sk-real", "q": "x"}).as_dict()
        self.assertEqual(out["data"]["args"]["api_key"], "[redacted]")
        self.assertEqual(out["data"]["args"]["q"], "x")

    def test_nested_secrets_are_scrubbed(self):
        out = scrub({"a": {"token": "t", "list": [{"password": "p"}]}})
        self.assertEqual(out["a"]["token"], "[redacted]")
        self.assertEqual(out["a"]["list"][0]["password"], "[redacted]")

    def test_long_strings_are_capped(self):
        out = scrub({"text": "y" * 100_000})
        self.assertLess(len(out["text"]), 100_000)
        self.assertIn("omitted", out["text"])

    def test_event_dict_is_json_serializable(self):
        payload = Emitter().emit(ev.TOOL_COMPLETED, tool="t", n=1).as_dict()
        json.dumps(payload)


# --- the loop ---


def offered(payload):
    """The tool names a request offered.

    The answer pass keeps `plan` and drops everything else, so "offered no
    tools" is no longer how a stub tells the answering call apart.
    """
    return {t.get("function", {}).get("name") for t in payload.get("tools") or []}


def answer_pass(payload):
    return not (offered(payload) - {"plan"})


class StubModel:
    """llama-server's /api/chat, reduced to the replies that matter."""

    def __init__(self, calls_per_round=1, answer="final answer", fail=None):
        self.calls_per_round = calls_per_round
        self.answer = answer
        self.fail = fail
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
                if stub.fail:
                    self.send_response(500)
                    body = stub.fail.encode()
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
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
        rounds = sum(1 for p in self.payloads if not answer_pass(p))
        out = [json.dumps({"message": {"thinking": "considering"}})]
        if answer_pass(payload) or self.calls_per_round == 0:
            out.append(json.dumps({"message": {"content": self.answer}}))
            return out
        for i in range(self.calls_per_round):
            out.append(json.dumps({"message": {"content": "", "tool_calls": [{
                "id": f"c{rounds}_{i}",
                "function": {"name": "web_search", "arguments": {"query": "eris"}},
            }]}}))
        return out


class LoopChecks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.stub = StubModel()
        self.old_url = settings.model_base_url
        self.old_searx = settings.searxng_url
        settings.model_base_url = self.stub.start()
        settings.searxng_url = "http://127.0.0.1:1"
        builtin.register_builtins()

    def tearDown(self):
        settings.model_base_url = self.old_url
        settings.searxng_url = self.old_searx
        self.stub.stop()

    def drive(self, request):
        engine = Engine(request)

        async def run():
            collected = []
            async for e in engine.run():
                collected.append(e)
            return collected, engine.result

        return asyncio.run(run())

    def request(self, **kw):
        base = dict(
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}],
            model="stub",
            tags=("chat",),
            budget=Budget(rounds=2, model_calls=20, tool_calls=20, seconds=60),
            tool_context=ToolContext(user_id=1),
        )
        base.update(kw)
        return RunRequest(**base)

    def test_a_tool_round_feeds_back_and_ends_in_an_answer(self):
        events, result = self.drive(self.request())
        kinds = [e.type for e in events]
        self.assertIn(ev.TOOL_STARTED, kinds)
        # SearXNG is not reachable here, so the search ends in failure; what the
        # round owes the loop is a terminal event either way.
        self.assertTrue({ev.TOOL_COMPLETED, ev.TOOL_FAILED} & set(kinds), kinds)
        self.assertEqual(result.answer.strip(), "final answer")
        self.assertEqual(result.stop_reason, "answered")

    @staticmethod
    def join_messages(payload):
        return " ".join(str(m.get("content") or "") for m in payload["messages"])

    def test_the_plan_comes_back_on_the_next_round(self):
        """The run's own steps ride on the tail of the prompt, and the stored
        history never gains the reminder, so a later round does not double it."""
        ctx = ToolContext(user_id=1, plan=({"step": "Read the docs", "status": "done"},
                                           {"step": "Answer", "status": "doing"}))
        self.drive(self.request(tool_context=ctx))
        first = self.join_messages(self.stub.payloads[0])
        self.assertIn("Read the docs", first)
        self.assertIn("[doing] Answer", first)
        # Every message the engine built itself, checked on the last round: the
        # reminder is appended per request, never stored in the list.
        stored = [m for m in self.stub.payloads[-1]["messages"]
                  if "The plan you recorded" in str(m.get("content") or "")]
        self.assertEqual(len(stored), 1, stored)

    def test_a_run_with_no_plan_sends_no_reminder(self):
        self.drive(self.request())
        self.assertNotIn("The plan you recorded", self.join_messages(self.stub.payloads[0]))

    def test_the_last_call_keeps_only_the_plan_tool(self):
        self.drive(self.request())
        self.assertTrue(offered(self.stub.payloads[0]) - {"plan"},
                        "the first call offered only the plan")
        self.assertEqual(offered(self.stub.payloads[-1]), {"plan"},
                         "the final call still offered the searching tools")

    def test_the_last_call_asks_for_no_thinking(self):
        """A reasoning model with no tools can reason to the token limit and
        answer nothing, which reaches the user as "No answer came back" with
        the prose sitting in a thinking card. The pass that must produce the
        answer is the one that must not be asked to think first."""
        self.drive(self.request(thinking=True))
        self.assertTrue(self.stub.payloads[0].get("think"))
        self.assertFalse(self.stub.payloads[-1].get("think"),
                         "the final call still asked the model to think")

    def test_round_budget_is_enforced(self):
        events, result = self.drive(self.request())
        rounds = {e.data.get("round") for e in events if e.type == ev.TOOL_STARTED}
        self.assertEqual(max(rounds), 1, f"rounds ran: {sorted(rounds)}")
        self.assertEqual(result.rounds, 3, "2 tool rounds plus the final answer")

    def test_the_tool_budget_stops_the_loop_cleanly(self):
        events, result = self.drive(self.request(budget=Budget(rounds=5, tool_calls=2,
                                                               model_calls=20, seconds=60)))
        failed = [e for e in events if e.type == ev.TOOL_FAILED]
        self.assertTrue(failed, "a spent tool budget produced no event")
        # An earlier round's search fails on its own here, because nothing is
        # listening on the search URL, so the budget refusal is looked up by
        # its code rather than taken as the first failure.
        codes = [e.data.get("code") for e in failed]
        self.assertIn(errors.BUDGET_EXCEEDED, codes, codes)

    # --- rounds earned by finding something new ---

    def searching(self, url_for_round):
        """A stub that always searches, against a tool whose sources are chosen
        per search. `url_for_round(n)` decides what the nth search returns."""
        from app.tools.registry import Tool, registry as shared

        calls = {"n": 0}

        def handler(args):
            url = url_for_round(calls["n"])
            calls["n"] += 1
            return ToolResult(success=True, text=f"read {url}",
                              metadata={"sources": [{"source": url, "kind": "web",
                                                     "title": url}]})

        shared.add(Tool(name="srch", description="d",
                        parameters={"type": "object", "properties": {}},
                        handler=handler, tags=("chat",)))
        self.addCleanup(shared.remove, "srch")

        class Searcher(StubModel):
            def reply(self, payload):
                if answer_pass(payload):
                    return [json.dumps({"message": {"content": "done"}})]
                return [json.dumps({"message": {"content": "", "tool_calls": [{
                    "id": f"c{len(self.payloads)}",
                    "function": {"name": "srch", "arguments": {}},
                }]}})]

        self.stub.stop()
        self.stub = Searcher()
        settings.model_base_url = self.stub.start()
        return calls

    def test_a_round_that_finds_a_new_source_earns_another(self):
        """The allowance is a floor, not a ceiling: a search that returned
        pages the run did not have is a search that worked, so it buys another."""
        self.searching(lambda n: f"https://example.com/{n}")
        events, result = self.drive(self.request(
            budget=Budget(rounds=1, extra_rounds=2, model_calls=20, tool_calls=20,
                          seconds=60)))
        rounds = {e.data.get("round") for e in events if e.type == ev.TOOL_STARTED}
        # 1 allowed + 2 earned = 3 searching rounds, then the answer.
        self.assertEqual(sorted(rounds), [0, 1, 2], f"rounds ran: {sorted(rounds)}")
        self.assertEqual(result.stop_reason, "answered")
        self.assertEqual(result.answer, "done")

    def test_a_round_that_finds_nothing_new_ends_the_search(self):
        """The same query returns the same pages. Asking again would spend the
        rest of the budget to learn nothing, so the run stops searching and
        writes what it has."""
        self.searching(lambda n: "https://example.com/same")
        events, result = self.drive(self.request(
            budget=Budget(rounds=1, extra_rounds=5, model_calls=20, tool_calls=20,
                          seconds=60)))
        rounds = {e.data.get("round") for e in events if e.type == ev.TOOL_STARTED}
        self.assertEqual(sorted(rounds), [0, 1], f"rounds ran: {sorted(rounds)}")
        self.assertEqual(result.stop_reason, "answered")
        self.assertEqual(result.answer, "done")

    def test_extra_rounds_cannot_spend_the_answer_call(self):
        """However much searching is on offer, one model call is held back for
        the answer: a run that exhausts its calls mid-search ends with nothing
        to show, which is worse than a shorter search."""
        self.searching(lambda n: f"https://example.com/{n}")
        _, result = self.drive(self.request(
            budget=Budget(rounds=1, extra_rounds=5, model_calls=3, tool_calls=20,
                          seconds=60)))
        self.assertEqual(result.answer, "done")
        self.assertEqual(result.budget["model_calls"], 3)

    def test_a_round_that_only_thinks_is_asked_again(self):
        """The 4B writes its whole answer into the thinking channel now and
        then and emits no content. The loop ends on a round with no tool calls,
        so without a second ask the turn reaches the user as nothing at all."""
        class Silent(StubModel):
            def reply(self, payload):
                # The second call for a round is the one without thinking.
                if payload.get("think") is False and answer_pass(payload):
                    return [json.dumps({"message": {"content": "recovered answer"}})]
                return [json.dumps({"message": {"thinking": "planning the reply"}})]

        self.stub.stop()
        self.stub = Silent(calls_per_round=0)
        settings.model_base_url = self.stub.start()
        _, result = self.drive(self.request(thinking="max"))
        self.assertEqual(result.answer, "recovered answer")
        self.assertEqual(result.stop_reason, "answered")
        self.assertEqual(len(self.stub.payloads), 2)
        self.assertFalse(self.stub.payloads[-1].get("think"))
        self.assertNotIn("web_search", json.dumps(self.stub.payloads[-1].get("tools") or []))

    def test_an_empty_round_with_no_thinking_is_not_retried(self):
        """A model that says nothing at all is not the thinking-channel bug;
        asking it the same question twice spends a call to get the same silence."""
        class Mute(StubModel):
            def reply(self, payload):
                return [json.dumps({"message": {"content": ""}})]

        self.stub.stop()
        self.stub = Mute(calls_per_round=0)
        settings.model_base_url = self.stub.start()
        _, result = self.drive(self.request(thinking="max"))
        self.assertEqual(result.answer, "")
        self.assertEqual(len(self.stub.payloads), 1)

    def test_a_tool_call_written_as_text_never_reaches_the_answer(self):
        """A pass with no searching tools makes the model write the call as
        prose, because the server only parses them when the list is sent. What
        the reader gets is the prose around it, never the markup."""
        class Markdown(StubModel):
            def reply(self, payload):
                out = [json.dumps({"message": {"content": "Let me check. "}})]
                out.append(json.dumps({"message": {"content": "<tool_call>\n<function=web_search>"}}))
                out.append(json.dumps({"message": {"content": "lost </tool_call> back to it."}}))
                return out

        self.stub.stop()
        self.stub = Markdown(calls_per_round=0)
        settings.model_base_url = self.stub.start()
        events, result = self.drive(self.request())
        text = "".join(e.data.get("text", "") for e in events if e.type == ev.MODEL_DELTA)
        self.assertNotIn("tool_call", text)
        self.assertEqual(result.answer, "Let me check.  back to it.")

    def test_an_answer_that_is_only_markup_is_asked_for_again(self):
        """Nothing but a call written as text is no answer at all, so the turn
        asks once more, and only the second pass is kept."""
        class OnlyMarkup(StubModel):
            def reply(self, payload):
                if "instead of an answer" in json.dumps(payload.get("messages") or []):
                    return [json.dumps({"message": {"content": "the real answer"}})]
                return [json.dumps({"message": {"content":
                    "<tool_call>\n<function=plan>\n<parameter=steps>\n[]\n</parameter>\n"
                    "</function>\n</tool_call>"}})]

        self.stub.stop()
        self.stub = OnlyMarkup(calls_per_round=0)
        settings.model_base_url = self.stub.start()
        _, result = self.drive(self.request())
        self.assertEqual(result.answer, "the real answer")
        self.assertEqual(len(self.stub.payloads), 2)

    def test_a_model_that_only_writes_markup_twice_is_told_so(self):
        """The second pass can fail the same way. An answer that is still
        markup is not shown: the turn closes with what happened instead."""
        class Stuck(StubModel):
            def reply(self, payload):
                return [json.dumps({"message": {"content":
                    "<tool_call><function=plan><parameter=steps>[]</parameter>"
                    "</function></tool_call>"}})]

        self.stub.stop()
        self.stub = Stuck(calls_per_round=0)
        settings.model_base_url = self.stub.start()
        _, result = self.drive(self.request())
        self.assertNotIn("tool_call", result.answer)
        self.assertIn("instead of an answer", result.answer)

    def test_a_final_pass_spent_on_the_plan_still_gets_an_answer(self):
        """The answering pass offers `plan`, and a model that records a step
        there leaves a card and no prose. The step is kept and the answer is
        asked for again, or the reader gets the card and "No answer came back"."""
        class Planner(StubModel):
            def reply(self, payload):
                if "You recorded the plan" in json.dumps(payload.get("messages") or []):
                    return [json.dumps({"message": {"content": "here is the answer"}})]
                if answer_pass(payload):
                    return [json.dumps({"message": {"content": "", "tool_calls": [{
                        "id": "p", "function": {"name": "plan", "arguments": {
                            "steps": [{"step": "Answer", "status": "doing"}]}},
                    }]}})]
                return [json.dumps({"message": {"content": "", "tool_calls": [{
                    "id": "s", "function": {"name": "web_search", "arguments": {"query": "x"}},
                }]}})]

        self.stub.stop()
        self.stub = Planner(calls_per_round=0)
        settings.model_base_url = self.stub.start()
        _, result = self.drive(self.request())
        self.assertEqual(result.answer, "here is the answer")
        self.assertEqual(result.stop_reason, "answered")

    def test_a_final_pass_that_plans_and_never_answers_says_so(self):
        """The second ask can fail the same way. A turn with a card and no prose
        is still not an answer, so the reader is told that instead."""
        class OnlyPlans(StubModel):
            def reply(self, payload):
                if answer_pass(payload):
                    return [json.dumps({"message": {"content": "", "tool_calls": [{
                        "id": "p", "function": {"name": "plan", "arguments": {
                            "steps": [{"step": "Answer", "status": "doing"}]}},
                    }]}})]
                return [json.dumps({"message": {"content": "", "tool_calls": [{
                    "id": "s", "function": {"name": "web_search", "arguments": {"query": "x"}},
                }]}})]

        self.stub.stop()
        self.stub = OnlyPlans(calls_per_round=0)
        settings.model_base_url = self.stub.start()
        _, result = self.drive(self.request())
        self.assertIn("never wrote an answer", result.answer)

    def test_a_model_error_is_not_a_success_shaped_answer(self):
        self.stub.fail = "boom"
        events, result = self.drive(self.request())
        self.assertEqual(result.stop_reason, "model_error")
        self.assertFalse(result.answer.strip())
        self.assertTrue(any(e.type == ev.TASK_FAILED for e in events))

    def test_an_unknown_tool_is_reported_not_run(self):
        class Unknown(StubModel):
            def reply(self, payload):
                if answer_pass(payload):
                    return [json.dumps({"message": {"content": "done"}})]
                return [json.dumps({"message": {"content": "", "tool_calls": [{
                    "id": "x", "function": {"name": "no_such_tool", "arguments": {}},
                }]}})]

        self.stub.stop()
        self.stub = Unknown()
        settings.model_base_url = self.stub.start()
        _, result = self.drive(self.request())
        self.assertEqual(result.tool_runs[0]["tool"], "no_such_tool")
        self.assertFalse(result.tool_runs[0]["ok"])
        self.assertEqual(result.tool_runs[0]["error"]["code"], errors.UNKNOWN_TOOL)

    def test_cancellation_between_steps(self):
        engine = Engine(self.request())

        async def run():
            out = []
            async for e in engine.run():
                out.append(e.type)
                if e.type in (ev.TOOL_COMPLETED, ev.TOOL_FAILED):
                    engine.cancel()
            return out, engine.result

        kinds, result = asyncio.run(run())
        self.assertEqual(result.stop_reason, "cancelled")
        self.assertIn(ev.TASK_CANCELLED, kinds)

    def test_a_state_changing_tool_is_refused_and_the_run_carries_on(self):
        """The tool is not offered, and naming it anyway does not get it run:
        the registry says no, the model is told, and the round ends in an
        answer. Nothing here can ask the user, so there is nothing to wait on."""
        from app.tools.registry import Tool, registry as shared

        shared.add(Tool(name="gated", description="d",
                        parameters={"type": "object", "properties": {}},
                        handler=lambda a: "did it", permission=perms.WRITE,
                        tags=("chat",)))

        class Gated(StubModel):
            def reply(self, payload):
                return [json.dumps({"message": {"content": "", "tool_calls": [{
                    "id": "g1", "function": {"name": "gated", "arguments": {}},
                }]}})]

        self.stub.stop()
        self.stub = Gated()
        settings.model_base_url = self.stub.start()
        request = self.request(tool_context=ToolContext(user_id=1))
        try:
            events, result = self.drive(request)
        finally:
            # The registry is process-wide, so a fixture tool registered here
            # would otherwise show up in every later test.
            shared.remove("gated")
        self.assertEqual(result.stop_reason, "answered")
        self.assertNotIn("gated", json.dumps(self.stub.payloads[0].get("tools") or []),
                         "a write was offered to the model")
        self.assertTrue(any(e.type == ev.TOOL_FAILED for e in events))
        self.assertFalse([r for r in result.tool_runs if r.get("ok")])

    def test_events_carry_increasing_sequence_numbers(self):
        events, _ = self.drive(self.request())
        seqs = [e.seq for e in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))

    def test_sources_from_tools_are_collected(self):
        events, result = self.drive(self.request())
        self.assertIsInstance(result.sources, list)

    def test_the_result_reports_what_was_spent(self):
        _, result = self.drive(self.request())
        self.assertEqual(result.budget["model_calls"], 3)
        self.assertGreaterEqual(result.budget["tool_calls"], 2)

    # --- calls within one round ---

    def tool_calls(self, calls):
        """A stub model that asks for every name in `calls` in one round.

        `calls` maps a tool name to `(handler, permission)`; each tool is
        registered for the test and removed when it ends, because the registry
        is process-wide.
        """
        from app.tools.registry import Tool, registry as shared

        for name, (handler, permission) in calls.items():
            shared.add(Tool(name=name, description="d",
                            parameters={"type": "object", "properties": {}},
                            handler=handler, permission=permission, tags=("chat",)))
            self.addCleanup(shared.remove, name)

        class Caller(StubModel):
            def reply(self, payload):
                if answer_pass(payload):
                    return [json.dumps({"message": {"content": "done"}})]
                return [json.dumps({"message": {"content": "", "tool_calls": [{
                    "id": n, "function": {"name": n, "arguments": {}},
                }]}}) for n in calls]

        self.stub.stop()
        self.stub = Caller()
        settings.model_base_url = self.stub.start()

    def test_a_round_runs_its_calls_together_and_reports_them_in_order(self):
        """The calls in a round are independent, so they run at once: a round
        that asked for a fast call and a slow one should cost the slow one, not
        the sum. What the model reads and what the browser is told must still
        follow the order it asked in, because the cards are matched by round."""
        order = []

        async def slow(args):
            await asyncio.sleep(0.15)
            order.append("slow")
            return ToolResult.ok("slow text")

        async def fast(args):
            order.append("fast")
            return ToolResult.ok("fast text")

        self.tool_calls({"slow": (slow, perms.READ_ONLY), "fast": (fast, perms.READ_ONLY)})
        start = time.monotonic()
        events, result = self.drive(self.request(
            budget=Budget(rounds=1, model_calls=20, tool_calls=20, seconds=60)))
        elapsed = time.monotonic() - start

        self.assertEqual(order, ["fast", "slow"], "the calls did not overlap")
        self.assertLess(elapsed, 0.26, "the round cost the sum of its calls")
        self.assertEqual([r["tool"] for r in result.tool_runs], ["slow", "fast"],
                         "tool_runs did not follow the order asked")
        completed = [e for e in events if e.type == ev.TOOL_COMPLETED]
        self.assertEqual([e.data["tool"] for e in completed], ["slow", "fast"],
                         "events did not follow the order asked")
        # The message the model reads follows the order it asked in too, so a
        # model that named its calls 1 and 2 is not answered 2 then 1.
        fed = [m for m in self.stub.payloads[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual([m["content"] for m in fed], ["slow text", "fast text"], fed)

    def test_an_idempotent_read_is_retried_and_a_state_change_is_not(self):
        """A network read that failed can be asked again; a call that changes
        something cannot, because the runtime cannot tell whether the first
        attempt took effect. The permission is what separates them.

        The write is refused by `authorize` before its handler runs, so its
        count stays at zero: the retry gate is the second reason it is not
        repeated, not the only one.
        """
        tries = {"read": 0, "write": 0, "final": 0}

        async def flaky_read(args):
            tries["read"] += 1
            if tries["read"] == 1:
                return ToolResult.failed(errors.TOOL_ERROR, "the server said 503", retryable=True)
            return ToolResult.ok("read it")

        async def flaky_write(args):
            tries["write"] += 1
            return ToolResult.failed(errors.TOOL_ERROR, "the write failed", retryable=True)

        async def final_answer(args):
            tries["final"] += 1
            return ToolResult.failed(errors.TOOL_ERROR, "no such address", retryable=False)

        self.tool_calls({
            "final": (final_answer, perms.EXTERNAL),
            "rd": (flaky_read, perms.EXTERNAL),
            "wr": (flaky_write, perms.WRITE),
        })
        _, result = self.drive(self.request(
            budget=Budget(rounds=1, model_calls=20, tool_calls=20, seconds=60)))
        self.assertEqual(tries["final"], 1, "an unretryable failure was retried")
        self.assertEqual(tries["read"], 2, "a retryable network read was not retried")
        # The write never ran at all: `authorize` refuses it before its handler.
        self.assertEqual(tries["write"], 0, "a state-changing call reached its handler")
        self.assertTrue([r for r in result.tool_runs if r["tool"] == "rd" and r["ok"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)