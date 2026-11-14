#!/usr/bin/env python3
"""Regression: absolute-form HTTP proxy requests must enter compression.

Uses only loopback mock endpoints and temporary HOME directories. Never sends
real conversations or credentials, or changes the user's running proxy/config.
Run: python -I tests/test_absolute_uri.py
"""
import ast
import http.client
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "proxy"


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((PROXY / "server.py").read_text(encoding="utf-8"))
        method = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "do_POST")
        method.decorator_list = []
        ns = {"log": logging.getLogger(__name__), "urlparse": urlparse}
        exec(compile(ast.Module(body=[method], type_ignores=[]),
                     str(PROXY / "server.py"), "exec"), ns)
        cls.dispatch = staticmethod(ns["do_POST"])

    def check_route(self, path, route, forwarded):
        class Request:
            def _handle_messages(self):
                self.route = "messages"

            def _proxy_raw(self, method):
                self.route = "raw"
                self.method = method

        req = Request()
        req.path = path
        self.dispatch(req)
        self.assertEqual(req.route, route)
        self.assertEqual(req.path, forwarded)
        if route == "raw":
            self.assertEqual(req.method, "POST")

    def test_origin_form(self):
        self.check_route("/v1/messages", "messages", "/v1/messages")

    def test_origin_form_query(self):
        self.check_route("/v1/messages?beta=true", "messages",
                         "/v1/messages?beta=true")

    def test_absolute_http(self):
        self.check_route("http://upstream.example:3456/v1/messages", "messages",
                         "/v1/messages")

    def test_absolute_query_preserves_encoding(self):
        self.check_route("http://upstream.example:3456/v1/messages?beta=true&x=a%2Fb",
                         "messages", "/v1/messages?beta=true&x=a%2Fb")

    def test_absolute_https(self):
        self.check_route("https://upstream.example/v1/messages?beta=true",
                         "messages", "/v1/messages?beta=true")

    def test_existing_count_tokens_route(self):
        self.check_route("http://upstream.example/v1/messages/count_tokens",
                         "messages", "/v1/messages/count_tokens")

    def test_other_post_routes_unchanged(self):
        for path in ("/v1/other?x=1", "http://upstream.example/v1/other?x=1"):
            with self.subTest(path=path):
                self.check_route(path, "raw", path)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, method, path, payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"content-type": "application/json", "x-api-key": "mock-only",
               "anthropic-version": "2023-06-01",
               "X-Claude-Code-Session-Id": "absolute-uri-regression"}
    try:
        conn.request(method, path,
                     body=json.dumps(payload).encode() if payload is not None else None,
                     headers=headers)
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            raise AssertionError(f"HTTP {response.status}: {body!r}")
        return body
    finally:
        conn.close()


class LiveCompressionTests(unittest.TestCase):
    def live_case(self, absolute, strict=False, delta_cache=False, no_space=False,
                  streaming=True, legacy_usage=False, expect_compression=True):
        with tempfile.TemporaryDirectory(prefix="rolling-context-absolute-uri-") as tmp:
            work = Path(tmp)
            (work / ".claude").mkdir()
            ports = set()
            while len(ports) < 3:
                ports.add(free_port())
            mock_port, proxy_port, mitm_port = sorted(ports)
            upstream = f"http://127.0.0.1:{mock_port}"
            (work / ".claude" / "settings.json").write_text(
                json.dumps({"env": {"ROLLING_CONTEXT_UPSTREAM": upstream}}))
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("ROLLING_CONTEXT_")
                   and k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
                   and not k.startswith("ANTHROPIC_")}
            env.update(HOME=tmp, USERPROFILE=tmp,
                       ROLLING_CONTEXT_UPSTREAM=upstream,
                       ROLLING_CONTEXT_PORT=str(proxy_port),
                       ROLLING_CONTEXT_MITM_PORT=str(mitm_port),
                       ROLLING_CONTEXT_TRIGGER="1000", ROLLING_CONTEXT_TARGET="400",
                       MOCK_STRICT="1" if strict else "0", MOCK_INPUT_TOKENS="150000")
            mock_log = work / "mock.jsonl"
            processes = []
            with (work / "process.log").open("wb") as output:
                try:
                    mock_script = ROOT / "tests" / "mock_endpoint.py"
                    source = mock_script.read_text(encoding="utf-8")
                    if delta_cache:
                        # Simulate a converter reporting almost all context as
                        # cached tokens, only in its final message_delta event.
                        old = ('"input_tokens": reported_input, "output_tokens": 1,\n'
                               '                          "cache_creation_input_tokens": 0,\n'
                               '                          "cache_read_input_tokens": 0')
                        self.assertIn(old, source)
                        source = source.replace(old, '"input_tokens": 0, "output_tokens": 1')
                        old = '"usage": {"output_tokens": 12}},'
                        self.assertIn(old, source)
                        source = source.replace(old,
                            '"usage": {"output_tokens": 12, "input_tokens": 3, '
                            '"cache_read_input_tokens": reported_input - 3}},')
                    if no_space:
                        source = source.replace('data: {}', 'data:{}')
                    if legacy_usage:
                        # Inclusive total below trigger; a double count would
                        # cross trigger and unnecessarily run compaction.
                        source = source.replace('"input_tokens": reported_input, "output_tokens": 1,',
                            '"input_tokens": 800, "output_tokens": 1, '
                            '"input_tokens_details": {"cached_tokens": 600},')
                        source = source.replace('"cache_read_input_tokens": 0',
                                                '"cache_read_input_tokens": 600')
                    mock_script = work / "mock_usage_variant.py"
                    mock_script.write_text(source, encoding="utf-8")
                    processes.append(subprocess.Popen(
                        [sys.executable, "-I", str(mock_script),
                         str(mock_port), str(mock_log)], cwd=tmp, env=env,
                        stdout=output, stderr=output))
                    # -I excludes implicit working-directory imports; explicitly
                    # load only this installed plugin's own modules.
                    bootstrap = ("import runpy,sys;sys.path.insert(0,sys.argv[1]);"
                                 "runpy.run_path(sys.argv[2],run_name='__main__')")
                    processes.append(subprocess.Popen(
                        [sys.executable, "-I", "-c", bootstrap, str(PROXY),
                         str(PROXY / "server.py")], cwd=tmp, env=env,
                        stdout=output, stderr=output))
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            request(proxy_port, "GET", "/health")
                            with socket.create_connection(("127.0.0.1", mock_port), 0.2):
                                pass
                            break
                        except (OSError, AssertionError):
                            if time.monotonic() >= deadline:
                                self.fail("mock/proxy did not start")
                            time.sleep(0.05)
                    messages = []
                    for i in range(24):
                        messages.extend([
                            {"role": "user", "content": f"question {i} " + "x" * 400},
                            {"role": "assistant", "content": f"answer {i} " + "y" * 400}])
                    messages.append({"role": "user", "content": "turn one"})
                    target = (upstream if absolute else "") + "/v1/messages?beta=true"
                    payload = {"model": "mock-model", "max_tokens": 64,
                               "stream": streaming, "messages": messages}
                    request(proxy_port, "POST", target, payload)
                    if not expect_compression:
                        deadline = time.monotonic() + 1
                        while time.monotonic() < deadline:
                            health = json.loads(request(proxy_port, "GET", "/health"))
                            self.assertEqual(health["compression_count"], 0)
                            self.assertEqual(health["active_compressions"], 0)
                            time.sleep(0.05)
                        log_text = (work / ".claude" / "rolling-context-debug.log").read_text()
                        self.assertIn("Input tokens from message_start: 800 ", log_text)
                        return
                    deadline = time.monotonic() + 15
                    while True:
                        health = json.loads(request(proxy_port, "GET", "/health"))
                        if health["compression_count"] and not health["active_compressions"]:
                            break
                        if time.monotonic() >= deadline:
                            self.fail(f"compression did not complete: {health}")
                        time.sleep(0.05)
                    turn2 = messages + [
                        {"role": "assistant", "content": "done"},
                        {"role": "user", "content": "turn two"}]
                    payload["messages"] = turn2
                    request(proxy_port, "POST", target, payload)
                    requests = [row["detail"] for row in
                                map(json.loads, mock_log.read_text().splitlines())
                                if row["kind"] == "request"]
                    chats = [r for r in requests if not r["compaction"]]
                    self.assertTrue(any(r["compaction"] for r in requests))
                    self.assertEqual(len(chats), 2)
                    self.assertEqual(chats[0]["path"], "/v1/messages?beta=true")
                    self.assertEqual(chats[1]["path"], "/v1/messages?beta=true")
                    self.assertTrue(chats[1]["carries_summary"])
                    original = len(json.dumps(turn2))
                    self.assertLess(chats[1]["convo_chars"], original * 0.75)
                    print(f"  {'absolute' if absolute else 'origin'}-form"
                          f"{' strict' if strict else ''}:"
                          f" {original:,} -> {chats[1]['convo_chars']:,} chars", flush=True)
                except Exception:
                    output.flush()
                    print((work / "process.log").read_text(errors="replace"),
                          file=sys.stderr)
                    raise
                finally:
                    for proc in reversed(processes):
                        proc.terminate()
                    for proc in reversed(processes):
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()

    def test_absolute_form_compresses_next_turn(self):
        self.live_case(absolute=True)

    def test_absolute_form_strict_endpoint_fallback(self):
        self.live_case(absolute=True, strict=True)

    def test_origin_form_still_compresses_next_turn(self):
        self.live_case(absolute=False)

    def test_absolute_form_final_cached_usage_triggers_compression(self):
        self.live_case(absolute=True, delta_cache=True)

    def test_sse_data_without_space_triggers_compression(self):
        self.live_case(absolute=True, no_space=True, delta_cache=True)

    def test_json_usage_triggers_compression(self):
        self.live_case(absolute=True, streaming=False)

    def test_legacy_cc_router_does_not_double_count_and_compress_early(self):
        self.live_case(absolute=True, legacy_usage=True, expect_compression=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
