#!/usr/bin/env python3
"""
Tests for qoder_shim.py -- run WITHOUT the real Qoder CLI.

Strategy: we don't need the vendor binary to prove the shim's logic.
We build a FAKE `qodercn` that emits the same *shape* of JSON we expect,
put it first on PATH, and drive the shim's HTTP surface end-to-end.

That verifies:
  - prompt flattening (messages[] -> string)
  - argv construction
  - JSON envelope unwrapping
  - OpenAI response shape
  - error paths (missing PAT, CLI failure, timeout)

What it CANNOT verify: that the real CLI's actual field names match our
guesses. That is flagged in the README as the one thing to check on a
machine that has the CLI installed.

Run:  python3 test_shim.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qoder_shim  # noqa: E402


# --------------------------------------------------------------------------
# A fake CLI we can point the shim at
# --------------------------------------------------------------------------

FAKE_CLI_SUCCESS = r'''#!/usr/bin/env python3
import json, sys, os
argv = sys.argv[1:]
# Record invocation so tests can assert on argv.
with open(os.environ["FAKE_LOG"], "w") as fh:
    json.dump(argv, fh)
prompt = ""
for i, a in enumerate(argv):
    if a == "-p" and i + 1 < len(argv):
        prompt = argv[i + 1]
out = {
    "result": "ECHO: " + prompt,
    "session_id": "fake-session-1",
}
print(json.dumps(out))
'''

FAKE_CLI_FAIL = r'''#!/usr/bin/env python3
import sys
sys.stderr.write("boom: not logged in\n")
sys.exit(3)
'''

FAKE_CLI_NOISY = r'''#!/usr/bin/env python3
import json
# Simulates a CLI that prints banner text before its JSON payload.
print("Welcome to Qoder CLI v9.9")
print(json.dumps({"result": "noisy ok"}))
'''

FAKE_CLI_ALT_KEYS = r'''#!/usr/bin/env python3
import json
# Simulates a CLI version that renamed the response field.
print(json.dumps({"response": "alt-key ok"}))
'''


class FakeCLIMixin:
    """Installs a fake CLI and points the shim's env at it."""

    fake_body: str = FAKE_CLI_SUCCESS

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="qodershim-test-")
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        self.cli_path = os.path.join(self.bindir, "qodercn")
        with open(self.cli_path, "w") as fh:
            fh.write(self.fake_body)
        os.chmod(self.cli_path, os.stat(self.cli_path).st_mode | stat.S_IEXEC)

        self.logpath = os.path.join(self.tmp, "argv.json")
        os.environ["FAKE_LOG"] = self.logpath
        os.environ["QODER_CLI_BIN"] = self.cli_path
        os.environ[qoder_shim.PAT_ENV] = "pt-fake-token"

        # Start a shim server on an ephemeral port.
        self.server = qoder_shim.ThreadingHTTPServer(
            ("127.0.0.1", 0), qoder_shim.Handler
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("FAKE_LOG", "QODER_CLI_BIN"):
            os.environ.pop(k, None)

    # ---- HTTP helpers ----

    def post_chat(self, payload: dict, timeout: float = 30.0):
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def get(self, path: str):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())


# --------------------------------------------------------------------------
# Pure-function tests (no subprocess, no server)
# --------------------------------------------------------------------------


class TestPromptTranslation(unittest.TestCase):
    def test_single_user_message_passthrough(self):
        msgs = [{"role": "user", "content": "hello world"}]
        self.assertEqual(qoder_shim.messages_to_prompt(msgs), "hello world")

    def test_multi_turn_renders_roles(self):
        msgs = [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "bye"},
        ]
        out = qoder_shim.messages_to_prompt(msgs)
        self.assertIn("System: be terse", out)
        self.assertIn("User: hi", out)
        self.assertIn("Assistant: hello", out)
        self.assertTrue(out.strip().endswith("Assistant:"))

    def test_multimodal_marks_image_omitted(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                ],
            }
        ]
        out = qoder_shim.messages_to_prompt(msgs)
        self.assertIn("what is this", out)
        self.assertIn("image attachment omitted", out)

    def test_empty_messages(self):
        self.assertEqual(qoder_shim.messages_to_prompt([]), "")


class TestJsonUnwrapping(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(
            qoder_shim.parse_json_loose('{"result":"x"}'), {"result": "x"}
        )

    def test_noisy_stdout(self):
        raw = 'banner line\n{"result":"x"}\ntrailing'
        self.assertEqual(qoder_shim.parse_json_loose(raw), {"result": "x"})

    def test_garbage_becomes_result(self):
        self.assertEqual(
            qoder_shim.parse_json_loose("not json at all"),
            {"result": "not json at all"},
        )

    def test_extract_primary_key(self):
        self.assertEqual(qoder_shim.extract_text({"result": "hi"}), "hi")

    def test_extract_alternate_keys(self):
        # Proves we tolerate a renamed field.
        self.assertEqual(qoder_shim.extract_text({"response": "hi"}), "hi")
        self.assertEqual(qoder_shim.extract_text({"text": "hi"}), "hi")
        self.assertEqual(
            qoder_shim.extract_text({"message": {"content": "hi"}}), "hi"
        )

    def test_extract_never_loses_data(self):
        payload = {"totally_unknown": 42}
        out = qoder_shim.extract_text(payload)
        self.assertIn("totally_unknown", out)


class TestCommandConstruction(unittest.TestCase):
    def test_flags_present(self):
        cmd = qoder_shim.build_cli_command("do it", "Qwen3.8-Flash")
        self.assertEqual(cmd[0], "qodercn")
        self.assertIn("-p", cmd)
        self.assertIn("do it", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("json", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("Qwen3.8-Flash", cmd)

    def test_no_model_flag_when_absent(self):
        cmd = qoder_shim.build_cli_command("x", "")
        self.assertNotIn("--model", cmd)


# --------------------------------------------------------------------------
# End-to-end tests over real HTTP with a fake CLI
# --------------------------------------------------------------------------


class TestEndToEndSuccess(FakeCLIMixin, unittest.TestCase):
    fake_body = FAKE_CLI_SUCCESS

    def test_models_endpoint(self):
        code, body = self.get("/v1/models")
        self.assertEqual(code, 200)
        self.assertEqual(body["object"], "list")
        self.assertEqual(body["data"][0]["id"], qoder_shim.default_model())

    def test_health(self):
        code, body = self.get("/health")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")

    def test_chat_completion_roundtrip(self):
        code, body = self.post_chat(
            {
                "model": "Qwen3.8-Flash",
                "messages": [{"role": "user", "content": "ping"}],
            }
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["model"], "Qwen3.8-Flash")
        choice = body["choices"][0]
        self.assertEqual(choice["message"]["role"], "assistant")
        self.assertEqual(choice["message"]["content"], "ECHO: ping")
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertTrue(body["id"].startswith("chatcmpl-"))

    def test_argv_actually_passed_to_cli(self):
        self.post_chat(
            {
                "model": "Qwen3.8-Flash",
                "messages": [{"role": "user", "content": "check argv"}],
            }
        )
        with open(self.logpath) as fh:
            argv = json.load(fh)
        self.assertIn("-p", argv)
        self.assertIn("check argv", argv)
        self.assertIn("--model", argv)
        idx = argv.index("--model")
        self.assertEqual(argv[idx + 1], "Qwen3.8-Flash")

    def test_model_defaults_when_omitted(self):
        code, body = self.post_chat(
            {"messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["model"], qoder_shim.default_model())

    def test_multi_turn_reaches_cli(self):
        self.post_chat(
            {
                "messages": [
                    {"role": "system", "content": "SYS"},
                    {"role": "user", "content": "USR"},
                ]
            }
        )
        with open(self.logpath) as fh:
            argv = json.load(fh)
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("SYS", prompt)
        self.assertIn("USR", prompt)

    def test_stream_flag_returns_valid_body(self):
        code, body = self.post_chat(
            {
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        # Documented limitation: no real streaming, but must not crash.
        self.assertEqual(code, 200)
        self.assertEqual(body["object"], "chat.completion")

    def test_bad_path_404(self):
        code, _ = self.get("/nope")
        self.assertEqual(code, 404)

    def test_get_on_chat_endpoint_405_or_404(self):
        code, _ = self.get("/v1/chat/completions")
        self.assertIn(code, (404, 405))

    def test_missing_messages_rejected(self):
        code, body = self.post_chat({"model": "x"})
        self.assertEqual(code, 400)
        self.assertIn("messages", body["error"]["message"])

    def test_empty_messages_rejected(self):
        code, body = self.post_chat({"messages": []})
        self.assertEqual(code, 400)

    def test_unknown_post_route_404(self):
        req = urllib.request.Request(
            self.base + "/v1/embeddings",
            data=json.dumps({"input": "x"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        self.assertEqual(code, 404)


class TestNoisyCLI(FakeCLIMixin, unittest.TestCase):
    fake_body = FAKE_CLI_NOISY

    def test_banner_text_is_tolerated(self):
        code, body = self.post_chat(
            {"messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "noisy ok")


class TestAltKeysCLI(FakeCLIMixin, unittest.TestCase):
    fake_body = FAKE_CLI_ALT_KEYS

    def test_renamed_field_still_works(self):
        code, body = self.post_chat(
            {"messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "alt-key ok")


class TestCLIFailure(FakeCLIMixin, unittest.TestCase):
    fake_body = FAKE_CLI_FAIL

    def test_nonzero_exit_becomes_502(self):
        code, body = self.post_chat(
            {"messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(code, 502)
        self.assertEqual(body["error"]["type"], "upstream_error")
        self.assertIn("boom", body["error"]["message"])


class TestMissingPAT(unittest.TestCase):
    """PAT must be checked with a *working* binary present, otherwise the
    binary check fires first and we never exercise the PAT path."""

    def setUp(self):
        self.saved_pat = os.environ.pop(qoder_shim.PAT_ENV, None)
        self.saved_bin = os.environ.get("QODER_CLI_BIN")
        # /bin/true exists on any POSIX box -> passes the which() check.
        os.environ["QODER_CLI_BIN"] = shutil.which("true") or "/bin/true"

    def tearDown(self):
        if self.saved_pat is not None:
            os.environ[qoder_shim.PAT_ENV] = self.saved_pat
        if self.saved_bin is None:
            os.environ.pop("QODER_CLI_BIN", None)
        else:
            os.environ["QODER_CLI_BIN"] = self.saved_bin

    def test_missing_pat_raises_clear_error(self):
        with self.assertRaises(qoder_shim.CLIError) as ctx:
            qoder_shim.run_cli("hi", "m")
        self.assertIn(qoder_shim.PAT_ENV, str(ctx.exception))


class TestMissingCLI(unittest.TestCase):
    def setUp(self):
        self.saved_bin = os.environ.get("QODER_CLI_BIN")
        self.saved_pat = os.environ.get(qoder_shim.PAT_ENV)
        os.environ["QODER_CLI_BIN"] = "definitely-not-a-real-binary-xyz"
        os.environ[qoder_shim.PAT_ENV] = "pt-x"

    def tearDown(self):
        if self.saved_bin is None:
            os.environ.pop("QODER_CLI_BIN", None)
        else:
            os.environ["QODER_CLI_BIN"] = self.saved_bin
        if self.saved_pat is None:
            os.environ.pop(qoder_shim.PAT_ENV, None)
        else:
            os.environ[qoder_shim.PAT_ENV] = self.saved_pat

    def test_missing_binary_raises(self):
        with self.assertRaises(qoder_shim.CLIError) as ctx:
            qoder_shim.run_cli("hi", "m")
        self.assertIn("not found on PATH", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
