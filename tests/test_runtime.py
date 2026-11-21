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
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")

from app.config import settings
from app import errors
from app.runtime import events as ev
from app.runtime.budgets import Budget, BudgetExceeded, clip
from app.runtime.engine import Engine, RunRequest
from app.runtime.events import emit
from app.runtime.routing import select_route
from app.security import permissions as perms
from app.security.workspace import Access
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
                          "fetch_url", "list_workspace", "plan", "read_file",
                          "screen", "wayback", "weather", "web_search",
                          "write_file"])

    def test_specs_carry_name_description_and_schema(self):
        spec = next(s for s in registry.specs_for(ToolContext(), ("chat",))
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
            return ToolResult.ok("rich")

        reg.add(Tool(name="t", description="d", parameters={"type": "object"},
                     handler=text_handler))
        reg.add(Tool(name="r", description="d", parameters={"type": "object"},
                     handler=result_handler))
        self.assertEqual(asyncio.run(reg.call("t", {})).text, "plain")
        self.assertEqual(asyncio.run(reg.call("r", {})).text, "rich")

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
            return f"files={len(ctx.files)}"

        reg.add(Tool(name="c", description="d", parameters={"type": "object"},
                     handler=needs_ctx))
        ctx = ToolContext(files=({"name": "a", "text": "b"},))
        got = asyncio.run(reg.call("c", {}, ctx))
        self.assertEqual(got.text, "files=1")


# --- permissions ---


class PolicyChecks(unittest.TestCase):
    def test_read_only_runs_without_a_project(self):
        tool = Tool(name="r", description="d", parameters={}, handler=lambda a: "",
                    permission=perms.READ_ONLY)
        self.assertIsNone(perms.authorize(tool))

    def test_a_write_is_refused_without_workspace_access(self):
        tool = Tool(name="w", description="d", parameters={}, handler=lambda a: "",
                    permission=perms.WRITE)
        code, reason = perms.authorize(tool)
        self.assertEqual(code, "denied")
        self.assertIn("workspace", reason)

    def test_a_write_requires_a_writable_workspace(self):
        tool = Tool(name="w", description="d", parameters={}, handler=lambda a: "",
                    permission=perms.WRITE)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(perms.authorize(tool, Access(Path(directory)))[0], "denied")
            self.assertIsNone(perms.authorize(tool, Access(Path(directory), writable=True)))

    def test_an_unknown_permission_is_refused(self):
        tool = Tool(name="x", description="d", parameters={}, handler=lambda a: "",
                    permission="god_mode")
        code, reason = perms.authorize(tool)
        self.assertEqual(code, "denied")
        self.assertIn("unknown permission", reason)

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
    def test_event_is_json_serializable(self):
        json.dumps({"type": ev.TOOL_COMPLETED, "data": emit(ev.TOOL_COMPLETED, tool="t", n=1).data})


# --- the loop ---


def offered(payload):
    """The tool names a request offered.

    The answer pass keeps `plan` and drops everything else, so "offered no
    tools" is no longer how a stub tells the answering call apart.
    """
    return {t.get("function", {}).get("name") for t in payload.get("tools") or []}


def answer_pass(payload):
    return not (offered(payload) - {"plan"})


def plain_pass(payload):
    """A pass offered nothing to call.

    The recovery ask drops the tool list entirely, so a stub can tell it apart
    from the answering pass, which still offers `plan`.
    """
    return not offered(payload)


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
            tool_context=ToolContext(),
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
        ctx = ToolContext(plan=({"step": "Read the docs", "status": "done"},
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

    def test_the_last_call_keeps_the_selected_effort(self):
        self.drive(self.request(thinking=True))
        self.assertTrue(self.stub.payloads[0].get("think"))
        self.assertTrue(self.stub.payloads[-1].get("think"))

    def test_round_budget_is_enforced(self):
        events, _ = self.drive(self.request())
        rounds = {e.data.get("round") for e in events if e.type == ev.TOOL_STARTED}
        self.assertEqual(max(rounds), 1, f"rounds ran: {sorted(rounds)}")
        # Every round reached, including the answer pass that carried no tool:
        # three model calls in all, the last of them the bare answer.
        spoke = {e.data.get("round") for e in events if e.type == ev.MODEL_DELTA}
        self.assertEqual(max(spoke), 2, f"rounds spoke: {sorted(spoke)}")

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
                if plain_pass(payload):
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
        # The model never wrote prose, so anything shown here came from the
        # engine telling the reader the turn produced no answer.
        self.assertNotIn("tool_call", result.answer)
        self.assertTrue(result.answer.strip())

    def test_a_final_pass_spent_on_the_plan_still_gets_an_answer(self):
        """The answering pass offers `plan`, and a model that records a step
        there leaves a card and no prose. The step is kept and the answer is
        asked for again, or the reader gets the card and "No answer came back"."""
        class Planner(StubModel):
            def reply(self, payload):
                if plain_pass(payload):
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
        self.assertNotIn("tool_call", result.answer)
        self.assertTrue(result.answer.strip())

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
        events, _ = self.drive(self.request())
        failed = [e for e in events if e.type == ev.TOOL_FAILED]
        self.assertTrue(failed, "an unknown tool produced no failure event")
        self.assertEqual(failed[0].data["tool"], "no_such_tool")
        self.assertEqual(failed[0].data["code"], errors.UNKNOWN_TOOL)

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
        request = self.request(tool_context=ToolContext())
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

    def test_a_round_runs_its_calls_together_and_reports_each_as_it_ends(self):
        """The calls in a round are independent, so they run at once: a round
        that asked for a fast call and a slow one should cost the slow one, not
        the sum.

        They are also reported as each one finishes, which is what lets a fast
        card close while a slow one is still running. What the model reads stays
        in the order it asked, because a model handed its results out of
        sequence answers its own calls out of sequence on the next round.
        """
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
        events, _ = self.drive(self.request(
            budget=Budget(rounds=1, model_calls=20, tool_calls=20, seconds=60)))
        elapsed = time.monotonic() - start

        self.assertEqual(order, ["fast", "slow"], "the calls did not overlap")
        self.assertLess(elapsed, 0.26, "the round cost the sum of its calls")
        completed = [e for e in events if e.type == ev.TOOL_COMPLETED]
        self.assertEqual([e.data["tool"] for e in completed], ["fast", "slow"],
                         "events did not follow completion")
        # The message the model reads follows the order it asked in, so a model
        # that named its calls 1 and 2 is not answered 2 then 1.
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
        self.drive(self.request(
            budget=Budget(rounds=1, model_calls=20, tool_calls=20, seconds=60)))
        self.assertEqual(tries["final"], 1, "an unretryable failure was retried")
        self.assertEqual(tries["read"], 2, "a retryable network read was not retried")
        # The write never ran at all: `authorize` refuses it before its handler.
        self.assertEqual(tries["write"], 0, "a state-changing call reached its handler")


class RoutingChecks(unittest.TestCase):
    def drive(self, route, replies, *, context=None, tool_results=(), budget=None):
        builtin.register_builtins()
        prompts = []

        async def model(messages, *, tools, **kwargs):
            index = len(prompts)
            names = tuple(spec["function"]["name"] for spec in tools or ())
            prompts.append((names, list(messages)))
            for reply in replies[index]:
                yield reply

        async def run():
            request = RunRequest(
                messages=[{"role": "system", "content": "sys"},
                          {"role": "user", "content": "request"}],
                budget=budget or Budget(rounds=3, model_calls=7),
                tool_context=context, routing=route,
            )
            engine = Engine(request)
            events = [event async for event in engine.run()]
            return events, engine.result

        tool = AsyncMock(side_effect=tool_results)
        with (patch("app.runtime.engine.stream_completion", model),
              patch("app.runtime.engine.cached_capabilities", return_value=None),
              patch.object(registry, "call", tool)):
            events, result = asyncio.run(run())
        return prompts, events, result, tool

    @staticmethod
    def call(name, args):
        return ("tool_calls", [{"id": name, "function": {"name": name, "arguments": args}}])

    def test_routes_indonesian_prompts_and_carries_an_explicit_follow_up(self):
        self.assertEqual(select_route("Jelasin cara kerja SSE").tools, ())
        self.assertEqual(select_route("Apa itu Bitcoin?").tools, ())
        self.assertEqual(select_route("Cuaca Singapura besok?").tools, ("weather",))
        follow_up = select_route("Kalau besok?", previous="Cuaca Singapura hari ini?")
        self.assertEqual(follow_up.tools, ("weather",))
        self.assertEqual(select_route("Grafik BTC minggu ini").tools, ("crypto_trend",))
        self.assertEqual(select_route("Screener saham paling naik").tools, ("screen",))
        self.assertEqual(select_route("Ringkas https://example.com lalu bandingkan dengan lampiran",
                                      has_attachments=True).tools, ("fetch_url", "read_file", "plan"))
        self.assertEqual(select_route("Tulis catatan.md").tools, ("write_file",))
        self.assertEqual(select_route("Lanjutkan", previous="Tulis catatan.md").tools, ("web_search",))

    def test_widening_discards_the_unsourced_draft_and_unlocks_page_reads(self):
        route = select_route("Cuaca Singapura besok?")
        source = {"source": "https://example.com/forecast", "title": "Forecast", "kind": "web"}
        prompts, events, result, tool = self.drive(
            route,
            [[("delta", "unverified draft")],
             [self.call("web_search", {"query": "Singapore forecast tomorrow"})],
             [("delta", "answer with source")]],
            tool_results=[ToolResult.ok("forecast", sources=[source])],
        )
        self.assertEqual([p[0] for p in prompts],
                         [("weather",), ("weather", "web_search"), ("fetch_url", "web_search", "weather")])
        self.assertNotIn("unverified draft", [e.data.get("text") for e in events])
        self.assertEqual(result.answer, "answer with source")
        self.assertEqual(tool.await_count, 1)

    def test_a_short_clarification_does_not_trigger_widening(self):
        prompts, _, result, tool = self.drive(
            select_route("Cuaca besok?"), [[("delta", "Di kota mana?")]],
        )
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result.answer, "Di kota mana?")
        tool.assert_not_awaited()

    def test_invalid_arguments_get_one_focused_repair_round(self):
        route = select_route("Cuaca Singapura besok?")
        invalid = ToolResult.failed(errors.INVALID_ARGUMENTS, "'location' is required")
        prompts, _, result, tool = self.drive(
            route,
            [[self.call("weather", {})],
             [self.call("weather", {})],
             [self.call("weather", {"location": "Singapore"})],
             [("delta", "could not verify")]],
            tool_results=[invalid, invalid],
        )
        self.assertEqual([p[0] for p in prompts], [("weather",), ("weather",), ("web_search",), ()])
        self.assertIn("Correct it once", prompts[1][1][-1]["content"])
        self.assertEqual(tool.await_count, 2)
        self.assertEqual(result.answer, "could not verify")

    def test_corrected_arguments_can_complete_the_tool_call(self):
        invalid = ToolResult.failed(errors.INVALID_ARGUMENTS, "'location' is required")
        _, events, result, tool = self.drive(
            select_route("Cuaca Singapura besok?"),
            [[self.call("weather", {})],
             [self.call("weather", {"location": "Singapore"})],
             [("delta", "forecast from tool")]],
            tool_results=[invalid, ToolResult.ok("forecast")],
        )
        self.assertEqual(tool.await_args.args[1], {"location": "Singapore"})
        self.assertTrue(any(event.type == ev.TOOL_COMPLETED for event in events))
        self.assertEqual(result.answer, "forecast from tool")

    def test_even_a_permitted_write_is_rejected_if_it_was_not_offered(self):
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(workspace=Access(Path(directory), writable=True))
            _, events, _, tool = self.drive(
                select_route("Cari dokumentasi SQLite"),
                [[self.call("write_file", {"path": "x.txt", "content": "test"})],
                 [("delta", "could not verify")]],
                context=context,
            )
        tool.assert_not_awaited()
        self.assertIn(errors.TOOL_NOT_OFFERED, [event.data.get("code") for event in events])

    def test_a_dependent_write_is_offered_after_search_returns_data(self):
        route = select_route("Cari berita terbaru lalu simpan ke laporan.md")
        source = {"source": "https://example.com/article", "kind": "web", "read": True}
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(workspace=Access(Path(directory), writable=True))
            prompts, _, _, _ = self.drive(
                route,
                [[self.call("web_search", {"query": "news"})],
                 [self.call("write_file", {"path": "laporan.md", "content": "test"})],
                 [("delta", "saved")]],
                context=context,
                tool_results=[ToolResult.ok("news", sources=[source]), ToolResult.ok("saved")],
            )
        self.assertNotIn("write_file", prompts[0][0])
        self.assertIn("write_file", prompts[1][0])
        self.assertNotIn("fetch_url", prompts[1][0])

    def test_chart_then_prose_still_runs_the_explicit_news_request(self):
        route = select_route("Cek apakah harga BBCA.JK di atas MA50, lalu cari berita terbaru tentang BBCA.")
        self.assertEqual(route.tools[:2], ("equity_trend", "web_search"))
        source = {"kind": "web", "source": "https://example.com/bbca", "title": "BBCA news"}
        _, events, result, tool = self.drive(
            route,
            [[self.call("equity_trend", {"symbol": "BBCA.JK"})],
             [("delta", "Mencari berita terbaru BBCA…")],
             [("delta", "Berikut berita yang ditemukan.")]],
            tool_results=[
                ToolResult.ok("technical data", card={"symbol": "BBCA.JK", "candles": [[1]]}),
                ToolResult.ok("news result", sources=[source]),
            ],
        )
        self.assertEqual([call.args[0] for call in tool.await_args_list], ["equity_trend", "web_search"])
        self.assertEqual(tool.await_args.args[1], {"query": "BBCA latest news"})
        self.assertTrue(any(e.type == ev.SOURCE_FOUND and e.data["source"] == source["source"]
                            for e in events))
        self.assertEqual(result.answer, "Berikut berita yang ditemukan.")

    def test_news_gets_the_reserved_tool_at_the_round_limit(self):
        route = select_route("Cek grafik BBCA.JK lalu cari berita terbaru.")
        technical_search = self.call("web_search", {"query": "BBCA MA50 hari ini"})
        technical_search[1][0]["id"] = "technical-search"
        prompts, _, result, tool = self.drive(
            route,
            [[self.call("equity_trend", {"symbol": "BBCA.JK"}), technical_search],
             [("delta", "Chart and news summary")]],
            tool_results=[
                ToolResult.ok("technical data", card={"symbol": "BBCA.JK", "candles": [[1]]}),
                ToolResult.failed(errors.TOOL_ERROR, "News service unavailable"),
            ],
            budget=Budget(rounds=1, extra_rounds=0, model_calls=2, tool_calls=2),
        )
        self.assertEqual([call.args[0] for call in tool.await_args_list], ["equity_trend", "web_search"])
        self.assertEqual(tool.await_args.args[1], {"query": "BBCA.JK latest news"})
        self.assertTrue(any("News service unavailable" in m.get("content", "") for m in prompts[-1][1]))
        self.assertEqual(result.answer, "Chart and news summary")

    def test_news_follow_up_uses_the_previous_ticker_and_avoids_quote_lookup(self):
        route = select_route("Cari berita terbaru", previous="Tolong cek grafik BBCA.JK")
        self.assertEqual(route.tools, ("web_search",))
        self.assertEqual(route.news_hint, "BBCA.JK latest news")
        self.assertEqual(select_route("Cari berita terbaru soal NVDA", previous="Grafik BBCA.JK").news_query,
                         "NVDA latest news")
        self.assertIsNone(select_route("Cek grafik BBCA.JK, jangan cari berita terbaru").news_query)

    def test_a_model_news_search_with_category_does_not_repeat(self):
        route = select_route("Cek grafik BBCA.JK lalu cari berita terbaru.")
        _, _, result, tool = self.drive(
            route,
            [[self.call("equity_trend", {"symbol": "BBCA.JK"})],
             [self.call("web_search", {"query": "BBCA", "categories": "news"})],
             [("delta", "Chart and news summary")]],
            tool_results=[ToolResult.ok("technical data"), ToolResult.ok("news result")],
            budget=Budget(rounds=2, extra_rounds=0, model_calls=3, tool_calls=2),
        )
        self.assertEqual([call.args[0] for call in tool.await_args_list], ["equity_trend", "web_search"])
        self.assertEqual(tool.await_args.args[1], {"query": "BBCA", "categories": "news"})
        self.assertEqual(result.answer, "Chart and news summary")


if __name__ == "__main__":
    unittest.main(verbosity=2)
