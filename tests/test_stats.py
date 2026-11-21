"""Runnable checks: python tests/test_stats.py

The server readout's two pieces of arithmetic, and the frame that feeds it.
The KV figure is the one number here that is derived rather than reported, so
it is checked against a model whose architecture makes the answer checkable by
hand — and against a real measurement taken from the same family.
"""

import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, ".")

from app.models import ladder, stats
from app.models.metadata import configured_context, model_context
from app.models.providers import (
    _call_metrics, _chat_payload, normalize_call, parse_chunk, parse_metrics, stream_completion,
)
from app.runtime.context import history_budget, recent


def info(**over):
    """The architecture of a small model: 4 layers, 2 KV heads, 64-wide."""
    base = {
        "layers": 4, "kv_heads": 2, "key_length": 64, "value_length": 64,
        "context": 32768,
    }
    base.update(over)
    return base


# Spark X2.6: 36 layers, one in four attending over the whole context.
SPARK_PATTERN = [True, True, True, False] * 9


def spark(**over):
    base = {
        "layers": 36, "kv_heads": 4, "key_length": 256, "value_length": 256,
        "window": 512, "window_pattern": SPARK_PATTERN, "context": 1048576,
    }
    base.update(over)
    return base


class KvChecks(unittest.TestCase):
    """What the cache costs, from the model's own metadata."""

    def test_a_dense_model_pays_for_every_layer_at_every_token(self):
        # 4 layers * 2 heads * (64 + 64) * 2 bytes = 2048 B/token
        self.assertEqual(stats.kv_bytes(info(), 1000), 4 * 2 * 128 * 2 * 1000)

    def test_a_windows_pattern_pays_flat_for_the_layers_it_caps(self):
        # Layers 1 and 3 are windowed: they stop at 512 tokens and add nothing
        # past it. Layers 0 and 2 keep growing.
        got = stats.kv_bytes(info(window=512, window_pattern=[False, True, False, True]), 4096)
        per_layer = 2 * 128 * 2
        self.assertEqual(got, 2 * per_layer * 512 + 2 * per_layer * 4096)

    def test_a_windows_pattern_is_measured_at_the_window_when_the_context_is_smaller(self):
        # Below the window the capped layers are not capped yet, so every layer
        # costs the same as any other.
        got = stats.kv_bytes(info(window=512, window_pattern=[False, True, False, True]), 128)
        self.assertEqual(got, stats.kv_bytes(info(), 128))

    def test_a_pattern_that_does_not_divide_the_layers_is_declined(self):
        # A repeating pattern shorter than the layer count is outside what this
        # arithmetic was checked against; it says so rather than guessing.
        got = stats.kv_bytes(info(window=512, window_pattern=[True, False]), 4096)
        self.assertIsNone(got)

    def test_a_model_without_a_window_pays_densely(self):
        got = stats.kv_bytes(info(), 4096)
        self.assertEqual(got, 4 * 2 * 128 * 2 * 4096)

    def test_a_model_that_hides_its_shape_gets_no_figure(self):
        self.assertIsNone(stats.kv_bytes(info(kv_heads=None), 4096))
        self.assertIsNone(stats.kv_bytes({}, 4096))

    def test_a_zero_context_is_not_a_figure(self):
        self.assertIsNone(stats.kv_bytes(info(), 0))

    def test_the_shape_is_found_under_whatever_prefix_the_model_uses(self):
        # The endpoint keys metadata by architecture, and the architecture
        # differs per model, so the prefix is discovered rather than assumed.
        got = stats.architecture({
            "spark2_5.block_count": 36,
            "spark2_5.attention.head_count_kv": 4,
            "spark2_5.attention.key_length": 256,
            "spark2_5.attention.value_length": 256,
            "spark2_5.attention.sliding_window": 512,
            "spark2_5.attention.sliding_window_pattern": [True] * 27 + [False] * 9,
            "spark2_5.context_length": 1048576,
        })
        self.assertEqual(got["layers"], 36)
        self.assertEqual(got["kv_heads"], 4)
        self.assertEqual(got["window"], 512)
        self.assertNotIn("key_length", stats.architecture({"other.embedding_length": 1}))

    def test_the_figure_matches_what_the_runner_actually_grows_by(self):
        """The 4.45B Spark model, against a measurement from the same machine.

        Its video-memory use grew by 37957 B/token between two context sizes,
        read from the GPU counters rather than from the endpoint. The formula
        has to land on that, or the panel is reporting a number nobody checked.
        """
        per_token = stats.kv_bytes(spark(), 32768) - stats.kv_bytes(spark(), 2048)
        per_token /= 32768 - 2048
        measured = 37957
        self.assertLess(abs(per_token - measured) / measured, 0.06,
                        f"{per_token:.0f} B/token is not within 6% of {measured}")

    def test_the_figure_scales_with_the_cache_type(self):
        """A quantized cache is not the f16 cache. The ratio is per element.

        q8_0 packs 32 elements plus one f32 scale into 34 bytes, and q4_0 packs
        32 plus one f16 scale into 18 — so the ratios are 0.53 and 0.28, not the
        0.5 and 0.25 a naive halving would give.
        """
        dense = info()
        f16 = stats.kv_bytes(dense, 4096, "f16")
        q8 = stats.kv_bytes(dense, 4096, "q8_0")
        q4 = stats.kv_bytes(dense, 4096, "q4_0")
        self.assertAlmostEqual(q8 / f16, 34 / 64, places=6)
        self.assertAlmostEqual(q4 / f16, 18 / 64, places=6)

    def test_an_unknown_cache_type_gets_no_figure(self):
        # A typo must not read as f16: a wrong figure that looks measured is
        # worse than a blank one that is honestly blank.
        self.assertIsNone(stats.kv_bytes(info(), 4096, "q6_k"))
        self.assertIsNone(stats.element_bytes("nonsense"))

    def test_the_cache_type_name_is_matched_case_insensitively(self):
        self.assertEqual(
            stats.kv_bytes(info(), 4096, "Q8_0"),
            stats.kv_bytes(info(), 4096, "q8_0"),
        )

    def test_a_quantized_cache_still_honours_a_window(self):
        per_layer_q8 = 2 * 128 * (34 / 32)
        got = stats.kv_bytes(
            info(window=512, window_pattern=[False, True, False, True]), 4096, "q8_0"
        )
        self.assertEqual(got, int(round(2 * per_layer_q8 * 512 + 2 * per_layer_q8 * 4096)))

    def test_the_metadata_prefix_is_read_off_a_real_payload(self):
        # The same shape, as /api/show actually spells it.
        got = stats.architecture({
            "spark2_5.block_count": 36,
            "spark2_5.attention.head_count_kv": 4,
            "spark2_5.attention.key_length": 256,
            "spark2_5.attention.value_length": 256,
            "spark2_5.attention.sliding_window": 512,
            "spark2_5.attention.sliding_window_pattern": SPARK_PATTERN,
            "spark2_5.context_length": 1048576,
            "spark2_5.embedding_length": 2560,
        })
        self.assertEqual(got, spark())


class MetricsChecks(unittest.TestCase):
    """The counts the server states once per call, and never otherwise."""

    def test_the_closing_frame_reports_what_the_call_cost(self):
        line = (
            '{"message":{"role":"assistant","content":""},"done":true,'
            '"prompt_eval_count":497,"prompt_eval_cached_count":60,"eval_count":29}'
        )
        kinds = dict(parse_chunk(line))
        self.assertEqual(kinds["metrics"]["prompt_eval_count"], 497)
        self.assertEqual(kinds["metrics"]["prompt_eval_cached_count"], 60)

    def test_a_partial_frame_carries_no_counts(self):
        self.assertEqual(parse_chunk('{"message":{"content":"hi"},"done":false}'), [("delta", "hi")])
        self.assertIsNone(parse_metrics({"message": {"content": "hi"}}))

    def test_compatible_usage_reports_prompt_completion_and_cached_subset(self):
        reported = parse_metrics({"usage": {"prompt_tokens": 900, "completion_tokens": 120,
                                            "prompt_tokens_details": {"cached_tokens": 600}}})
        self.assertEqual(reported, {"prompt_eval_count": 900, "eval_count": 120,
                                    "prompt_eval_cached_count": 600})

    def test_compatible_tool_arguments_are_joined_before_parsing(self):
        fragments = {}
        first = '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"t","function":{"name":"weather","arguments":"{\\"loc"}}]}}]}'
        second = '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ation\\":\\"Jakarta\\"}"}}]}}]}'
        self.assertEqual(parse_chunk(first, tool_fragments=fragments), [])
        self.assertEqual(parse_chunk(second, tool_fragments=fragments), [])
        self.assertEqual(normalize_call(fragments[0])["function"]["arguments"], {"location": "Jakarta"})

    def test_the_first_frame_reports_an_empty_write_and_nothing_else(self):
        events = parse_chunk('{"message":{"content":""},"done":true,"eval_count":0}')
        self.assertEqual(events, [("metrics", {"eval_count": 0})])

    def test_a_string_where_a_count_belongs_is_dropped(self):
        self.assertIsNone(parse_metrics({"done": True, "prompt_eval_count": "497"}))
        self.assertEqual(stats._num("497"), 497)
        self.assertIsNone(stats._num("many"))

    def test_the_frame_with_no_counts_at_all_is_not_a_metrics_event(self):
        self.assertIsNone(parse_metrics({"done": True}))
        self.assertEqual(parse_chunk('{"message":{"content":"bye"},"done":true}'), [("delta", "bye")])


class ReasoningChecks(unittest.TestCase):
    def test_exact_names_and_default_come_from_metadata(self):
        declared = ladder._ollama({"thinking": {"values": ["economy", "intensive"], "default": "economy"},
                                   "capabilities": ["completion"]})
        self.assertEqual(declared.values, ("economy", "intensive"))
        self.assertEqual(declared.default, "economy")
        self.assertIsNone(ladder.request_effort(declared, None))

    def test_legacy_switch_is_based_on_capabilities(self):
        self.assertEqual(ladder._ollama({"capabilities": ["thinking"]}).values, (False, True))
        self.assertEqual(ladder._ollama({"capabilities": ["completion"]}).values, ())
        self.assertEqual(ladder._ollama({}).source, "unavailable")

    def test_llama_levels_and_fixed_window_use_props(self):
        profile = ladder._llama({
            "chat_template_caps": {"reasoning_effort_levels": ["low", "high", "none"],
                                   "reasoning_effort_rejected": ["none"]},
            "default_generation_settings": {"params": {"reasoning_effort": "low"}},
        })
        self.assertEqual(profile.values, ("low", "high"))
        with patch("app.models.providers.cached_reasoning", return_value=profile):
            body = _chat_payload([], "local", "high", None, 16000, None, compatible=True)
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertNotIn("options", body)

    def test_off_stays_off_when_native_call_has_tools(self):
        profile = ladder.Reasoning((False, True), True, "metadata")
        with patch("app.models.providers.cached_reasoning", return_value=profile):
            body = _chat_payload([], "local", False, [{"type": "function"}], 8192, None)
            default = _chat_payload([], "local", None, None, None, None)
        self.assertIs(body["think"], False)
        self.assertEqual(body["options"]["num_ctx"], 8192)
        self.assertNotIn("think", default)


class ContextChecks(unittest.IsolatedAsyncioTestCase):
    async def test_actual_window_is_joined_to_completed_metrics(self):
        with patch("app.models.providers.runtime_status", new_callable=AsyncMock,
                   side_effect=[{"context_length": 8192}, {"context_length": 16384}]):
            first = await _call_metrics({"prompt_eval_count": 900}, "a", 8192, None)
            second = await _call_metrics({"prompt_eval_count": 1000}, "a", None, None)
        self.assertEqual((first["context_window"], first["context_source"]), (8192, "runner"))
        self.assertEqual(second["context_window"], 16384)

    async def test_a_later_runner_allocation_is_not_used_for_a_different_request(self):
        with patch("app.models.providers.runtime_status", new_callable=AsyncMock,
                   return_value={"context_length": 16384}):
            usage = await _call_metrics({"prompt_eval_count": 1000}, "a", 4096, None)
        self.assertEqual((usage["context_window"], usage["context_source"]), (4096, "requested"))

    async def test_training_limit_is_never_used_as_runner_capacity(self):
        with (patch("app.models.stats.model_info", new_callable=AsyncMock,
                    return_value={"model_info": {"qwen.context_length": 131072}}),
              patch("app.models.stats.runner_report", new_callable=AsyncMock, return_value=(True, None)),
              patch("app.models.stats.gpu_info", new_callable=AsyncMock, return_value={})):
            report = await stats.model_stats("a")
        self.assertEqual(report["context"]["max"], 131072)
        self.assertIsNone(report["context"]["allocated"])
        self.assertIsNone(report["kv"]["ctx"])

    async def test_unknown_capacity_stays_unknown_and_request_is_marked(self):
        with (patch("app.models.providers.cached_model_info", return_value={}),
              patch("app.models.providers.runtime_status", new_callable=AsyncMock, return_value=None)):
            unknown = await _call_metrics({"prompt_eval_count": 900}, "a", None, None)
            wanted = await _call_metrics({"prompt_eval_count": 900}, "a", 4096, None)
        self.assertIsNone(unknown["context_window"])
        self.assertEqual((wanted["context_window"], wanted["context_source"]), (4096, "requested"))

    def test_context_metadata_and_adaptive_history_keep_tool_pairs(self):
        shown = {"model_info": {"general.architecture": "qwen", "qwen.context_length": 131072,
                                "vision.context_length": 7936}, "parameters": "num_ctx   16384\n"}
        self.assertEqual(model_context(shown), 131072)
        self.assertEqual(configured_context(shown), 16384)
        system = {"role": "system", "content": "instructions"}
        messages = [
            {"role": "user", "content": "x" * 4000},
            {"role": "user", "content": "latest query"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "one"}]},
            {"role": "tool", "content": "é" * 4000, "tool_call_id": "one"},
        ]
        small = recent(messages, history_budget(1024, system))
        large = recent(messages, history_budget(8192, system))
        self.assertEqual([m["role"] for m in small], ["user", "assistant", "tool"])
        self.assertEqual(small[-1]["tool_call_id"], "one")
        self.assertLess(len(small[-1]["content"]), len(messages[-1]["content"]))
        self.assertEqual(large, messages)

    async def test_llama_uses_compatible_stream_with_its_fixed_window(self):
        requests = []
        async_client = httpx.AsyncClient

        def handle(request):
            requests.append((request.url.path, json.loads(request.content)))
            if request.url.path == "/api/chat":
                return httpx.Response(404)
            frame = {"choices": [{"delta": {"content": "answer"}}],
                     "usage": {"prompt_tokens": 11, "completion_tokens": 3}}
            return httpx.Response(200, text=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n")

        def mock_client(**kwargs):
            return async_client(transport=httpx.MockTransport(handle), **kwargs)

        props = {"default_generation_settings": {"n_ctx": 8192}}
        profile = ladder.Reasoning(("low", "high"), "low", "metadata")
        with (patch("app.models.providers.cached_model_info", return_value=None),
              patch("app.models.providers.cached_properties", return_value=None),
              patch("app.models.providers.cached_reasoning", return_value=profile),
              patch("app.models.providers.server_properties", new_callable=AsyncMock, return_value=props),
              patch("app.models.providers.httpx.AsyncClient", side_effect=mock_client)):
            events = dict([event async for event in stream_completion(
                [{"role": "user", "content": "q"}], model="llama", thinking="high", ctx=65536
            )])
        self.assertEqual([path for path, _ in requests], ["/api/chat", "/v1/chat/completions"])
        self.assertEqual(requests[-1][1]["reasoning_effort"], "high")
        self.assertNotIn("options", requests[-1][1])
        self.assertEqual(events["metrics"]["context_window"], 8192)
        self.assertEqual(events["delta"], "answer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
