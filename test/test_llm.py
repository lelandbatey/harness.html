"""llm.py: retries with backoff + jitter, timeouts, error classification, model listing, per-session
provider resolution. Uses tiny in-process HTTP servers."""
import http.server
import json
import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src", "py"), os.path.join(ROOT, "vendor")]

from sfh import llm  # noqa: E402

SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "AI_ENDPOINT", "AI_MODEL", "AI_TOKEN", "AI_CHEAP_MODEL")


def serve(handler):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
    return httpd


def provider(port, **kw):
    return dict({"id": "p", "type": "openai", "base_url": "http://127.0.0.1:%d/v1" % port, "model": "m",
                 "cheap_model": "m", "api_key_credential": None}, **kw)


SSE_OK = (b'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
          b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')


class Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in SECRET_ENV if k in os.environ}

    def tearDown(self):
        os.environ.update(self.saved)

    def test_backoff_has_jitter_and_grows(self):
        d1 = [llm.backoff_delay(1, 2.0) for _ in range(200)]
        d3 = [llm.backoff_delay(3, 2.0) for _ in range(200)]
        self.assertTrue(all(1.0 <= d <= 3.0 for d in d1))
        self.assertTrue(all(4.0 <= d <= 12.0 for d in d3))
        self.assertGreater(len(set(d1)), 50)  # jittered, not a fixed schedule
        self.assertLessEqual(max(llm.backoff_delay(20, 2.0) for _ in range(50)), 45)

    def test_retry_policy_bounds(self):
        self.assertEqual(llm.retry_policy({}), (3, 2.0, 30, 120))
        self.assertEqual(llm.retry_policy({"max_retries": -4, "retry_base_delay": "x", "request_timeout": 0})[:3],
                         (0, 2.0, 0.1))

    def test_retries_on_5xx_then_succeeds(self):
        calls = []

        class H(Quiet):
            def do_POST(self):
                calls.append(time.time())
                self.rfile.read(int(self.headers["content-length"]))
                if len(calls) < 3:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"overloaded"}}')
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                self.wfile.write(SSE_OK)
        httpd = serve(H)
        try:
            s = llm.retrying_stream({"max_retries": 3, "retry_base_delay": 0.01}, provider(httpd.server_port), {},
                                    None, "sys", [{"role": "user", "content": [{"type": "text", "text": "x"}]}])
            out = list(s)
        finally:
            httpd.shutdown()
        retries = [e for e in out if e[0] == "retry"]
        self.assertEqual([(e[1], e[2]) for e in retries], [(1, 3), (2, 3)])
        self.assertIn("503", retries[0][4])
        self.assertIn(("delta", 0, "text", "hi"), out)
        self.assertEqual(out[-1], ("stop", "end_turn"))
        self.assertEqual(len(calls), 3)

    def test_client_errors_are_not_retried(self):
        calls = []

        class H(Quiet):
            def do_POST(self):
                calls.append(1)
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"bad model"}}')
        httpd = serve(H)
        try:
            s = llm.retrying_stream({"max_retries": 3, "retry_base_delay": 0.01}, provider(httpd.server_port), {},
                                    None, "", [{"role": "user", "content": [{"type": "text", "text": "x"}]}])
            with self.assertRaises(llm.LLMError) as cm:
                list(s)
        finally:
            httpd.shutdown()
        self.assertFalse(cm.exception.retryable)
        self.assertIn("bad model", str(cm.exception))
        self.assertEqual(len(calls), 1)

    def test_request_timeout_then_give_up(self):
        calls = []

        class Slow(Quiet):
            def do_POST(self):
                calls.append(1)
                time.sleep(1.5)  # never answers within the 0.3s request timeout
        httpd = serve(Slow)
        try:
            s = llm.retrying_stream({"max_retries": 2, "retry_base_delay": 0.01, "request_timeout": 0.3},
                                    provider(httpd.server_port), {}, None, "",
                                    [{"role": "user", "content": [{"type": "text", "text": "x"}]}])
            t = time.time()
            with self.assertRaises(llm.LLMError) as cm:
                list(s)
            elapsed = time.time() - t
        finally:
            httpd.shutdown()
        self.assertIn("did not respond within", str(cm.exception))
        self.assertIn("gave up after 3 attempts", str(cm.exception))
        self.assertEqual(len(calls), 3)
        self.assertLess(elapsed, 3)

    def test_stalled_stream_is_retried(self):
        calls = []

        class Stall(Quiet):
            def do_POST(self):
                calls.append(1)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                if len(calls) == 1:  # starts answering, then goes silent past the idle timeout
                    self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"content":"par"}}]}\n\n')
                    self.wfile.flush()
                    time.sleep(1.5)
                    return
                self.wfile.write(SSE_OK)
        httpd = serve(Stall)
        try:
            s = llm.retrying_stream({"max_retries": 2, "retry_base_delay": 0.01, "stream_idle_timeout": 0.3},
                                    provider(httpd.server_port), {}, None, "",
                                    [{"role": "user", "content": [{"type": "text", "text": "x"}]}])
            out = list(s)
        finally:
            httpd.shutdown()
        kinds = [e[0] for e in out]
        self.assertLess(kinds.index("delta"), kinds.index("retry"))  # partial output came before the retry
        self.assertIn("stopped sending data", [e for e in out if e[0] == "retry"][0][4])
        self.assertEqual(out[-1], ("stop", "end_turn"))

    def test_close_interrupts_retry_wait(self):
        def make(attempt):
            raise llm.LLMError("down", retryable=True)
        s = llm.RetryingStream(make, retries=5, base_delay=30)
        it = iter(s)
        self.assertEqual(next(it)[0], "retry")
        threading.Timer(0.1, s.close).start()
        t = time.time()
        self.assertEqual(list(it), [])
        self.assertLess(time.time() - t, 2)

    def test_mock_flaky(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "[[flaky:1]] hello"}]}]
        s = llm.retrying_stream({"retry_base_delay": 0.0}, dict(llm.MOCK), {}, None, "", msgs)
        out = list(s)
        self.assertEqual(sum(1 for e in out if e[0] == "retry"), 1)
        self.assertEqual(out[-1], ("stop", "end_turn"))

    def test_complete_retries(self):
        calls = []

        class H(Quiet):
            def do_POST(self):
                calls.append(1)
                self.rfile.read(int(self.headers["content-length"]))
                if len(calls) == 1:
                    self.send_response(429)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": "title!"}}]}).encode())
        httpd = serve(H)
        try:
            out = llm.complete(provider(httpd.server_port), {}, "p", settings={"retry_base_delay": 0.01})
        finally:
            httpd.shutdown()
        self.assertEqual(out, "title!")
        self.assertEqual(len(calls), 2)


class UsageTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(llm.normalize_usage({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
                                              "cache_creation_input_tokens": 7}, "anthropic"),
                         {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 100, "cache_write_tokens": 7})
        self.assertEqual(llm.normalize_usage({"prompt_tokens": 722, "completion_tokens": 60, "prompt_tokens_details":
                                              {"cached_tokens": 22, "cache_write_tokens": 0}, "cost": 0.0012}, "openai"),
                         {"input_tokens": 700, "output_tokens": 60, "cache_read_tokens": 22, "cache_write_tokens": 0,
                          "cost": 0.0012})
        self.assertIsNone(llm.normalize_usage(None, "openai"))

    def test_add_usage(self):
        t = llm.add_usage(None, {"input_tokens": 1, "output_tokens": 2, "cost": 0.5})
        t = llm.add_usage(t, {"input_tokens": 3, "output_tokens": 4})
        self.assertEqual((t["input_tokens"], t["output_tokens"], t["calls"], t["priced_calls"], t["cost"]),
                         (4, 6, 2, 1, 0.5))

    def test_pricing_parse_and_lookup(self):
        table = llm.parse_pricing({"builtin": {"Claude-X": {"inputPerMTok": 4, "outputPerMTok": 20,
                                                            "cacheReadPerMTok": 0.2, "cacheWritePerMTok": 5},
                                               "partial": {"inputPerMTok": 1}},
                                   "overrides": {"claude-x": {"outputPerMTok": 30}}})
        self.assertEqual(table, {"claude-x": {"input": 4, "output": 30, "cache_read": 0.2, "cache_write": 5}})
        self.assertEqual(llm.parse_pricing([{"model": "a", "input": 1, "output": 2}]), {"a": {"input": 1, "output": 2}})
        for name in ("claude-x", "anthropic/claude-x", "claude-x[1m]", "claude-x-20250514", "Anthropic/Claude-X"):
            self.assertIs(llm.find_price(table, name), table["claude-x"], name)
        self.assertIsNone(llm.find_price(table, "claude-y"))
        u = {"input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 10000, "cache_write_tokens": 200}
        self.assertAlmostEqual(llm.usage_cost(u, table["claude-x"]), (4000 + 3000 + 2000 + 1000) / 1e6)
        self.assertAlmostEqual(llm.usage_cost(u, {"input": 1, "output": 2}), (1000 + 200 + 10000 + 200) / 1e6)

    def test_pricing_urls(self):
        self.assertEqual(llm.pricing_urls(provider(5), {}), ["http://127.0.0.1:5/settings/api/pricing",
                                                             "http://127.0.0.1:5/v1/settings/api/pricing"])
        self.assertEqual(llm.pricing_urls(provider(5, pricing_url=""), {}), [])
        self.assertEqual(llm.pricing_urls(provider(5, pricing_url="http://x/p"), {}), ["http://x/p"])
        self.assertEqual(llm.pricing_urls({"type": "mock"}, {}), [])

    def test_fetch_pricing(self):
        class H(Quiet):
            def do_GET(self):
                if self.path != "/settings/api/pricing":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"builtin":{"m":{"inputPerMTok":1,"outputPerMTok":2}},"overrides":{}}')
        httpd = serve(H)
        try:
            self.assertEqual(llm.fetch_pricing(provider(httpd.server_port), {}), {"m": {"input": 1, "output": 2}})
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertIsNone(llm.fetch_pricing(provider(httpd.server_port), {}, timeout=1))  # unreachable

    def test_add_usage_counts_estimates(self):
        t = llm.add_usage(None, {"input_tokens": 1, "cost": 0.5, "cost_estimated": True})
        t = llm.add_usage(t, {"input_tokens": 1, "cost": 0.25})
        self.assertEqual((t["priced_calls"], t["estimated_calls"], t["cost"]), (2, 1, 0.75))

    def test_openai_stream_reports_usage(self):
        seen = {}

        class H(Quiet):
            def do_POST(self):
                seen["body"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'
                                 b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3,"cost":0.002}}\n\n'
                                 b'data: [DONE]\n\n')
        httpd = serve(H)
        try:
            out = list(llm.stream(provider(httpd.server_port), {}, None, "", [
                {"role": "user", "content": [{"type": "text", "text": "x"}]}]))
        finally:
            httpd.shutdown()
        self.assertEqual(seen["body"]["stream_options"], {"include_usage": True})
        self.assertEqual(out[-2:], [("usage", {"input_tokens": 12, "output_tokens": 3, "cache_read_tokens": 0,
                                               "cache_write_tokens": 0, "cost": 0.002}), ("stop", "end_turn")])

    def test_anthropic_stream_reports_usage(self):
        events = [("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 9, "output_tokens": 1,
                                                                              "cache_read_input_tokens": 4}}}),
                  ("content_block_start", {"type": "content_block_start", "index": 0,
                                           "content_block": {"type": "text", "text": ""}}),
                  ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                           "delta": {"type": "text_delta", "text": "yo"}}),
                  ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                     "usage": {"output_tokens": 15}}),
                  ("message_stop", {"type": "message_stop"})]

        class H(Quiet):
            def do_POST(self):
                self.rfile.read(int(self.headers["content-length"]))
                self.send_response(200)
                self.end_headers()
                for name, d in events:
                    self.wfile.write(("event: %s\ndata: %s\n\n" % (name, json.dumps(d))).encode())
        httpd = serve(H)
        try:
            p = {"id": "a", "type": "anthropic", "base_url": "http://127.0.0.1:%d" % httpd.server_port,
                 "model": "c", "cheap_model": "c", "api_key_credential": "AK"}
            out = list(llm.stream(p, {"AK": "k"}, None, "", [{"role": "user", "content": [{"type": "text", "text": "x"}]}]))
        finally:
            httpd.shutdown()
        self.assertEqual(out[-2], ("usage", {"input_tokens": 9, "output_tokens": 15, "cache_read_tokens": 4,
                                             "cache_write_tokens": 0}))


class ModelsTest(unittest.TestCase):
    def test_list_models_openai(self):
        seen = {}

        class H(Quiet):
            def do_GET(self):
                seen["path"], seen["auth"] = self.path, self.headers.get("authorization")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"object": "list", "data": [
                    {"id": "zeta"}, {"id": "alpha", "display_name": "Alpha 1"}, {"id": "alpha"}, "plain"]}).encode())
        httpd = serve(H)
        try:
            out = llm.list_models(provider(httpd.server_port, api_key_credential="K"), {"K": "tok"})
        finally:
            httpd.shutdown()
        self.assertEqual(seen, {"path": "/v1/models", "auth": "Bearer tok"})
        self.assertEqual([m["id"] for m in out], ["alpha", "plain", "zeta"])

    def test_list_models_anthropic_and_mock(self):
        seen = {}

        class H(Quiet):
            def do_GET(self):
                seen["path"], seen["key"] = self.path, self.headers.get("x-api-key")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"id": "claude-x", "display_name": "Claude X"}]}).encode())
        httpd = serve(H)
        try:
            p = {"id": "a", "type": "anthropic", "base_url": "http://127.0.0.1:%d" % httpd.server_port,
                 "model": "claude-x", "cheap_model": "claude-x", "api_key_credential": "AK"}
            out = llm.list_models(p, {"AK": "k1"})
        finally:
            httpd.shutdown()
        self.assertEqual(seen, {"path": "/v1/models?limit=1000", "key": "k1"})
        self.assertEqual(out, [{"id": "claude-x", "name": "Claude X"}])
        self.assertEqual(llm.list_models(dict(llm.MOCK), {}), [{"id": "mock", "context_window": 200000, "efforts": ["low", "medium", "high", "max"]}])

    def test_model_override(self):
        settings = {"model_overrides": {
            "p": {"claude-opus[1m]": {"context_window": 200000}, "bad": {"context_window": "x"},
                  "zero": {"context_window": 0}},
            "*": {"claude-opus[1m]": {"context_window": 500000}, "bad": {"context_window": 100000},
                  "anthropic/glm": {"context_window": 64000}}}}
        mo = llm.model_override
        self.assertEqual(mo(settings, "p", "claude-opus[1m]"), {"context_window": 200000})   # provider wins
        self.assertEqual(mo(settings, "q", "claude-opus[1m]"), {"context_window": 500000})   # "*" for others
        self.assertEqual(mo(settings, "p", "bad"), {"context_window": 100000})   # invalid falls through to "*"
        self.assertEqual(mo(settings, "p", "zero"), {})
        self.assertEqual(mo(settings, "p", "glm"), {"context_window": 64000})    # prefixed ids match
        self.assertEqual(mo(settings, "p", "other"), {})
        self.assertEqual(mo(settings, "p", None), {})
        for junk in (None, [], "x", {"p": []}, {"p": {"m": 5}}):
            self.assertEqual(mo({"model_overrides": junk}, "p", "m"), {})
        self.assertEqual(llm.find_model([{"id": "a/b"}, {"id": "c"}], "b"), {"id": "a/b"})
        self.assertEqual(llm.find_model([{"id": "c"}], "x/c"), {"id": "c"})
        self.assertIsNone(llm.find_model([{"id": "c"}], "d"))

    def test_list_models_unreachable(self):
        p = provider(1)
        with self.assertRaises(llm.LLMError):
            llm.list_models(p, {}, timeout=2)


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in SECRET_ENV if k in os.environ}

    def tearDown(self):
        os.environ.update(self.saved)

    def test_session_provider_resolution(self):
        settings = {"provider": "local", "model": "override", "cheap_model": None, "providers": [
            {"id": "local", "type": "openai", "base_url": "http://x/v1", "model": "small"},
            {"id": "other", "type": "openai", "base_url": "http://y/v1", "model": "big"}]}
        self.assertEqual(llm.resolve_provider(settings, {})["model"], "override")
        # the global model override belongs to the globally selected provider only
        self.assertEqual(llm.resolve_provider(settings, {}, "other")["model"], "big")
        self.assertEqual(llm.resolve_provider(settings, {}, "local")["model"], "override")
        with self.assertRaises(llm.LLMError):
            llm.resolve_provider(settings, {}, "nope")

    def test_thinking_from_another_model_is_dropped(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "q"}]},
                {"role": "assistant", "model": "old", "content": [
                    {"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "text", "text": "a"}]},
                {"role": "user", "content": [{"type": "text", "text": "q2"}]}]
        same = llm.sanitize_messages(msgs, "old")
        other = llm.sanitize_messages(msgs, "new")
        self.assertEqual([b["type"] for b in same[1]["content"]], ["thinking", "text"])
        self.assertEqual([b["type"] for b in other[1]["content"]], ["text"])



class EffortTest(unittest.TestCase):
    def test_bodies(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hi", "started_at": 1}]}]
        b = llm.build_body("m", "", msgs, effort="high")
        self.assertEqual((b["output_config"], b["thinking"]), ({"effort": "high"}, {"type": "adaptive"}))
        self.assertEqual(b["messages"][0]["content"], [{"type": "text", "text": "hi"}])  # no timing fields sent
        b = llm.build_body("m", "", msgs, thinking_budget=2000, effort="low", adaptive=False)
        self.assertEqual((b["output_config"], b["thinking"]["type"]), ({"effort": "low"}, "enabled"))
        b = llm.build_body("m", "", msgs, thinking_budget=2000)
        self.assertNotIn("output_config", b)
        self.assertEqual(llm.build_openai_body("m", "", msgs, effort="medium")["reasoning_effort"], "medium")
        self.assertNotIn("reasoning_effort", llm.build_openai_body("m", "", msgs))

    def test_model_efforts(self):
        caps = {"effort": {"supported": True, "max": {"supported": True}, "low": {"supported": True},
                           "high": {"supported": True}, "xhigh": {"supported": False}},
                "thinking": {"supported": True, "types": {"adaptive": {"supported": True}}}}
        self.assertEqual(llm.model_efforts({"capabilities": caps}), (["low", "high", "max"], True))
        self.assertEqual(llm.model_efforts({"capabilities": {"effort": {"supported": False},
                                                             "thinking": {"supported": False}}}), ([], False))
        self.assertEqual(llm.model_efforts({"supported_parameters": ["tools", "reasoning"]}),
                         (["low", "medium", "high"], None))
        self.assertEqual(llm.model_efforts({"supported_parameters": ["tools"]}), ([], None))
        self.assertEqual(llm.model_efforts({"reasoning_efforts": ["high", "minimal"]}), (["minimal", "high"], None))
        self.assertEqual(llm.model_efforts({"id": "x"}), (None, None))

    def test_clamp_and_override(self):
        self.assertEqual(llm.clamp_effort("high", None), "high")
        self.assertIsNone(llm.clamp_effort("high", []))
        self.assertIsNone(llm.clamp_effort(None, ["low"]))
        self.assertEqual(llm.clamp_effort("xhigh", ["low", "medium", "high", "max"]), "high")
        self.assertEqual(llm.clamp_effort("minimal", ["low", "high"]), "low")
        settings = {"model_overrides": {"*": {"m": {"efforts": ["high", "low"]}}, "p": {"n": {"efforts": []}}}}
        self.assertEqual(llm.model_override(settings, "p", "m"), {"efforts": ["low", "high"]})
        self.assertEqual(llm.model_override(settings, "p", "n"), {"efforts": []})

    def test_effort_rejected(self):
        self.assertTrue(llm.effort_rejected(llm.LLMError("Anthropic API error 400: output_config.effort: Extra inputs")))
        self.assertTrue(llm.effort_rejected(llm.LLMError("OpenAI-compatible API error 400: Unrecognized "
                                                         "request argument supplied: reasoning_effort")))
        self.assertFalse(llm.effort_rejected(llm.LLMError("Anthropic API error 400: messages: bad")))
        self.assertFalse(llm.effort_rejected(llm.LLMError("API error 500: effort", retryable=True)))
        self.assertFalse(llm.effort_rejected(ValueError("error 400 effort")))

if __name__ == "__main__":
    unittest.main()
