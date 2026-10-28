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
from app.tools import builtin, policy
from app.tools.registry import Registry, Tool, ToolContext, ToolResult, registry


# --- the tool contract ---


class RegistryChecks(unittest.TestCase):
    def setUp(self):
        builtin.register_builtins()

    def test_registry_is_idempotent(self):
        builtin.register_builtins()
        self.assertEqual(sorted(registry.names()),
                         ["calc", "fetch_url", "web_search"])

    def test_specs_carry_name_description_and_schema(self):
        spec = next(s for s in registry.specs(("chat",))
                    if s["function"]["name"] == "web_search")
        self.assertEqual(spec["type"], "function")
        self.assertTrue(spec["function"]["description"])
        self.assertEqual(spec["function"]["parameters"]["required"], ["query"])

    def test_invalid_arguments_are_refused_before_running(self):
        r = asyncio.run(registry.call("calc", {"expression": 12}))
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
                    permission=policy.READ_ONLY)
        self.assertTrue(policy.authorize(tool, ToolContext()).allowed)

    def test_a_write_needs_an_approval_for_these_arguments(self):
        tool = Tool(name="w", description="d", parameters={}, handler=lambda a: "",
                    permission=policy.WRITE)
        ctx = ToolContext()
        decision = policy.authorize(tool, ctx, {"proposal_id": "abc"})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "approval_required")

    def test_an_approval_is_bound_to_the_arguments(self):
        tool = Tool(name="w", description="d", parameters={}, handler=lambda a: "",
                    permission=policy.WRITE)
        token = perms.approval_token("w", {"proposal_id": "abc"})
        ctx = ToolContext(approved={token})
        self.assertTrue(policy.authorize(tool, ctx, {"proposal_id": "abc"}).allowed)
        # The same tool with different arguments is a different operation.
        self.assertFalse(policy.authorize(tool, ctx, {"proposal_id": "def"}).allowed)

    def test_an_unknown_permission_is_refused(self):
        tool = Tool(name="x", description="d", parameters={}, handler=lambda a: "",
                    permission="god_mode")
        decision = policy.authorize(tool, ToolContext())
        self.assertFalse(decision.allowed)
        self.assertIn("unknown permission", decision.reason)

    def test_approval_tokens_are_stable_and_distinct(self):
        a = perms.approval_token("t", {"b": 2, "a": 1})
        b = perms.approval_token("t", {"a": 1, "b": 2})
        c = perms.approval_token("t", {"a": 1, "b": 3})
        self.assertEqual(a, b, "key order changed the token")
        self.assertNotEqual(a, c)

    def test_specs_offer_what_can_be_asked_for_and_withhold_the_rest(self):
        reg = Registry()
        reg.add(Tool(name="read", description="d", parameters={"type": "object"},
                     handler=lambda a: "", permission=policy.READ_ONLY, tags=("chat",)))
        reg.add(Tool(name="write", description="d", parameters={"type": "object"},
                     handler=lambda a: "", permission=policy.WRITE, tags=("chat",)))

        # Both are offered: a write is unapproved, and asking is what puts the
        # approval in front of the user.
        names = {s["function"]["name"] for s in reg.specs_for(ToolContext())}
        self.assertEqual(names, {"read", "write"})


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
        rounds = sum(1 for p in self.payloads if p.get("tools"))
        out = [json.dumps({"message": {"thinking": "considering"}})]
        if not payload.get("tools") or self.calls_per_round == 0:
            out.append(json.dumps({"message": {"content": self.answer}}))
            return out
        for i in range(self.calls_per_round):
            out.append(json.dumps({"message": {"content": "", "tool_calls": [{
                "id": f"c{rounds}_{i}",
                "function": {"name": "calc", "arguments": {"expression": "1+1"}},
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
        self.assertIn(ev.TOOL_COMPLETED, kinds)
        self.assertEqual(result.answer.strip(), "final answer")
        self.assertEqual(result.stop_reason, "answered")

    def test_the_last_call_offers_no_tools(self):
        self.drive(self.request())
        self.assertTrue(self.stub.payloads[0].get("tools"))
        self.assertFalse(self.stub.payloads[-1].get("tools"),
                         "the final call still offered tools")

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
        self.assertEqual(failed[0].data["code"], errors.BUDGET_EXCEEDED)

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
                if not payload.get("tools"):
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

    def test_a_model_error_is_not_a_success_shaped_answer(self):
        self.stub.fail = "boom"
        events, result = self.drive(self.request())
        self.assertEqual(result.stop_reason, "model_error")
        self.assertFalse(result.answer.strip())
        self.assertTrue(any(e.type == ev.TASK_FAILED for e in events))

    def test_an_unknown_tool_is_reported_not_run(self):
        class Unknown(StubModel):
            def reply(self, payload):
                if not payload.get("tools"):
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
                if e.type == ev.TOOL_COMPLETED:
                    engine.cancel()
            return out, engine.result

        kinds, result = asyncio.run(run())
        self.assertEqual(result.stop_reason, "cancelled")
        self.assertIn(ev.TASK_CANCELLED, kinds)

    def test_a_required_approval_stops_the_run(self):
        """A gated call must surface as `approval_required`, not be smoothed
        over by a later answer: the operation is the thing the user has to see."""
        from app.tools.registry import Tool, registry as shared

        shared.add(Tool(name="gated", description="d",
                        parameters={"type": "object", "properties": {}},
                        handler=lambda a: "did it", permission=policy.WRITE,
                        tags=("chat",)))

        class Gated(StubModel):
            def reply(self, payload):
                if not payload.get("tools"):
                    return [json.dumps({"message": {"content": "I did the thing."}})]
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
        self.assertEqual(result.stop_reason, "approval_required")
        self.assertTrue(any(e.type == ev.APPROVAL_REQUIRED for e in events))
        # No second call: the run stopped rather than asking the model to keep
        # going without the thing it asked for.
        self.assertEqual(len(self.stub.payloads), 1)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)