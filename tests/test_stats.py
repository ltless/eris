"""Runnable checks: python tests/test_stats.py

The server readout's two pieces of arithmetic, and the frame that feeds it.
The KV figure is the one number here that is derived rather than reported, so
it is checked against a model whose architecture makes the answer checkable by
hand — and against a real measurement taken from the same family.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, ".")

from app.models import stats
from app.models.providers import parse_chunk, parse_metrics

ROOT = Path(__file__).resolve().parents[1]


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

    def test_the_first_frame_reports_an_empty_write_and_nothing_else(self):
        # A server that closes the stream with an empty message still states
        # the counts on it, so the frame is read for them rather than skipped.
        events = parse_chunk('{"message":{"content":""},"done":true,"eval_count":0}')
        self.assertEqual(events, [("metrics", {"eval_count": 0})])

    def test_a_string_where_a_count_belongs_is_dropped(self):
        # Only numbers are kept; a server that spells a count differently
        # loses the row rather than putting text where a number belongs.
        self.assertIsNone(parse_metrics({"done": True, "prompt_eval_count": "497"}))
        # The reader that turns the endpoint's own fields into numbers is
        # separate, because those can arrive as strings.
        self.assertEqual(stats._num("497"), 497)
        self.assertIsNone(stats._num("many"))

    def test_the_frame_with_no_counts_at_all_is_not_a_metrics_event(self):
        self.assertIsNone(parse_metrics({"done": True}))
        self.assertEqual(parse_chunk('{"message":{"content":"bye"},"done":true}'), [("delta", "bye")])


if __name__ == "__main__":
    unittest.main(verbosity=2)