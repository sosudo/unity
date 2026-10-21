"""Offline metric-aware input parsing; no compiler, model or service calls."""
from copy import deepcopy
import math
import sys
import unittest

from unity import optimize_metrics as metrics


class ScoreParsingTests(unittest.TestCase):
    def parse(self, builtin, value):
        return metrics.parse_score_value({"builtin": builtin}, value)

    def test_counts_accept_exact_nonnegative_integers_and_numeric_text(self):
        cases = ((0, 0), (18, 18), (18.0, 18), ("0", 0), ("1", 1),
                 ("18", 18), ("18.0", 18), (" +18.000 ", 18), ("1.8e1", 18),
                 ("9007199254740993", 9007199254740993),
                 ("9007199254740993.0", 9007199254740993))
        for builtin in ("length", "completion"):
            for value, expected in cases:
                with self.subTest(builtin=builtin, value=value):
                    actual = self.parse(builtin, value)
                    self.assertIs(type(actual), int)
                    self.assertEqual(actual, expected)

    def test_counts_do_not_round_truncate_or_accept_invalid_types(self):
        invalid = (True, False, -1, -0.5, 1.9, float("nan"), float("inf"),
                   float("-inf"), "-1", "-0.1", "1.9", "18.000000000000000001",
                   "NaN", "Infinity", "-Infinity", "1e-9999", "1e999999999",
                   "", " ", "eighteen", "18 tactics", "0x12", "1_000", "1,000",
                   "１８", None, {}, [], [18])
        for builtin in ("length", "completion"):
            for value in invalid:
                with self.subTest(builtin=builtin, value=value):
                    with self.assertRaises(ValueError):
                        self.parse(builtin, value)

    def test_large_count_range_matches_existing_validator_without_rounding(self):
        # These integers all pass the pre-existing finite-number check. In
        # particular, the largest float is not an artificial integer ceiling.
        for value in (10 ** 308, int(sys.float_info.max), int(sys.float_info.max) + 1):
            self.assertTrue(math.isfinite(value))
            for raw in (value, str(value)):
                with self.subTest(raw=raw):
                    self.assertEqual(self.parse("length", raw), value)
        for raw in (10 ** 309, "1e309", "1e999999999"):
            with self.subTest(rejected=raw):
                with self.assertRaises(ValueError):
                    self.parse("length", raw)

    def test_modularity_accepts_finite_ratios_without_integer_conversion(self):
        for value, expected in ((0, 0), (1, 1), (0.75, 0.75),
                                ("0", 0.0), ("1", 1.0), ("0.75", 0.75),
                                (" 7.5e-1 ", 0.75)):
            with self.subTest(value=value):
                parsed = self.parse("modularity", value)
                self.assertEqual(parsed, expected)
                self.assertIs(type(parsed), float if isinstance(value, str) else type(value))

    def test_modularity_rejects_invalid_or_out_of_range_values_before_rounding(self):
        for value in (True, False, -0.1, 1.1, "-0.00000000000000000001",
                      "1.00000000000000000001", "1e-9999", "NaN", "Infinity",
                      float("inf"), float("nan"), "75%", "", None, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.parse("modularity", value)

    def test_modularity_undefined_still_requires_zero_denominator_evidence(self):
        metric = metrics._sealed({"builtin": "modularity", "direction": "higher", "units": "ratio"})
        identity = metrics._sealed({"source": "fixture"})
        value = metrics.parse_score_value(metric, "undefined")
        self.assertEqual(value, "undefined")
        args = dict(subject="first", raw_value=value, source_identity=identity,
                    method={"description": "Typed have / tactics"})
        with self.assertRaisesRegex(ValueError, "ratio"):
            metrics.record_measurement(metric, **args, evidence={"reason": "not a zero denominator"})
        row = metrics.record_measurement(metric, **args, evidence={"zero_denominator": True})
        self.assertEqual(row["raw_value"], "undefined")
        self.assertEqual(row["evidence_kind"], "agent_judged")

    def test_custom_scores_preserve_numeric_and_qualitative_types_exactly(self):
        # Neither a direction nor an overridden builtin name declares a type.
        custom = {"name": "length", "builtin": None, "direction": "lower"}
        before = deepcopy(custom)
        for value in (18, 0.75, -1.25, "clearer", "18", "0.75", " 18 ", "undefined"):
            with self.subTest(value=value):
                result = metrics.parse_score_value(custom, value)
                self.assertIs(type(result), type(value))
                self.assertEqual(result, value)
        self.assertEqual(custom, before)

    def test_custom_scores_still_reject_invalid_scalar_inputs(self):
        for value in (True, False, float("nan"), float("inf"), "", " ", None, {}, []):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.parse(None, value)

    def test_sealed_count_validation_is_not_relaxed_by_input_parsing(self):
        metric = metrics._sealed({"builtin": "length", "direction": "lower", "units": "tactics"})
        identity = metrics._sealed({"source": "fixture"})
        kwargs = dict(subject="first", source_identity=identity,
                      method={"description": "Count fixture tactics"}, evidence={"reason": "fixture"})
        original = metrics.record_measurement(metric, raw_value=18, **kwargs)
        saved = deepcopy(original)
        for invalid in ("18", 18.0):
            with self.subTest(invalid=invalid):
                forged = deepcopy(original)
                forged["raw_value"] = invalid
                forged["sha256"] = metrics.digest({k: v for k, v in forged.items() if k != "sha256"})
                self.assertFalse(metrics.validate_measurement_binding(forged, metric, identity))
                with self.assertRaises(ValueError):
                    metrics.record_measurement(metric, raw_value=invalid, **kwargs)
        parsed = metrics.record_measurement(metric, raw_value=metrics.parse_score_value(metric, "18"), **kwargs)
        self.assertEqual(parsed, original)
        self.assertEqual(original, saved)


if __name__ == "__main__":
    unittest.main()
