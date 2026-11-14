#!/usr/bin/env python3
"""Offline regression tests for input token semantics and partial SSE usage."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("rc_usage", Path(__file__).resolve().parents[1] / "proxy" / "usage.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
InputUsage = module.InputUsage


class UsageTests(unittest.TestCase):
    def test_native_disjoint_fields(self):
        u = InputUsage()
        self.assertTrue(u.update({"input_tokens":3, "cache_read_input_tokens":80,
                                  "cache_creation_input_tokens":17}))
        self.assertEqual(u.total, 100)
        self.assertEqual(u.cache_read, 80)

    def test_legacy_cc_router_explicit_openai_details(self):
        u = InputUsage()
        u.update({"input_tokens":80000, "cache_read_input_tokens":60000,
                  "input_tokens_details":{"cached_tokens":60000}})
        self.assertEqual(u.total, 80000)
        self.assertEqual(u.fresh, 20000)

    def test_fixed_cc_router_without_details(self):
        u = InputUsage()
        u.update({"input_tokens":20000, "cache_read_input_tokens":60000})
        self.assertEqual(u.total, 80000)

    def test_partial_delta_preserves_start_cache(self):
        u = InputUsage()
        u.update({"input_tokens":3, "cache_read_input_tokens":80000})
        u.update({"input_tokens":10, "output_tokens":12})
        self.assertEqual(u.total, 80010)
        self.assertEqual(u.cache_read, 80000)
        self.assertFalse(u.update({"output_tokens":20}))
        self.assertEqual(u.total, 80010)

    def test_final_delta_cached_usage(self):
        u = InputUsage()
        u.update({"input_tokens":0})
        u.update({"input_tokens":"3", "cache_read_input_tokens":"99997"})
        self.assertEqual(u.total, 100000)

    def test_prompt_total_and_bad_split_are_bounded(self):
        u = InputUsage()
        u.update({"prompt_tokens":100, "prompt_tokens_details":{"cached_tokens":150}})
        self.assertEqual(u.total, 100)
        self.assertEqual(u.cache_read, 100)
        self.assertEqual(u.fresh, 0)

    def test_unknown_cache_is_not_false_miss(self):
        u = InputUsage()
        u.update({"input_tokens":100})
        self.assertIsNone(u.cache_read)
        u.update({"cache_read_input_tokens":0})
        self.assertEqual(u.cache_read, 0)

    def test_malformed_usage_does_not_raise(self):
        u = InputUsage()
        for bad in (None, [], "usage", {"input_tokens":None},
                    {"input_tokens":True}, {"input_tokens":-3},
                    {"input_tokens":float("inf")}, {"input_tokens":"invalid"}):
            self.assertFalse(u.update(bad))
        u.update({"input_tokens":"12", "cache_read_input_tokens":3.0})
        self.assertEqual(u.total, 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
