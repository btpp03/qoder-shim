#!/usr/bin/env python3
"""
Qoder CLI -> OpenAI-compatible shim
====================================

WHAT THIS IS (and what it is NOT)
---------------------------------
This is NOT a network reverse proxy. There is no upstream HTTP endpoint
that speaks OpenAI's /v1/chat/completions protocol, so there is nothing
to "proxy" in the classic sense.

Instead, Qoder CN exposes its model through a local CLI binary
(`qodercn`). This shim is a *protocol adapter*:

    your app  --HTTP-->  this shim  --subprocess-->  qodercn -p ...  --> Qoder

We translate:
    OpenAI chat request  ->  a prompt string + CLI flags
    CLI JSON output      ->  an OpenAI chat response object

That translation layer is the whole trick. Understanding it is the point.

WHY THE CLI AND NOT THE API
---------------------------
Qoder does publish an API: https://api.qoder.com.cn/api/v1/{cloud,forward}
But that API manages *Agents, Sessions, Events* (Agent-as-a-Service).
It has no /chat/completions-shaped endpoint. Verified: an unauthenticated
GET to /api/v1/cloud/agents returns
    401 {"code":"TOKEN_INVALID","message":"missing authorization token"}
which confirms real Bearer auth but no chat surface.

So we drive the CLI, which IS documented for headless automation:
    qodercn -p "<prompt>" --output-format json

SCOPE / CAVEATS -- read these
-----------------------------
1. PROMO WINDOW: Qwen3.8-Flash is free (0.1x -> 0.0x credits) only from
   2026-09-18 10:00 to 2026-09-30 23:59:59 (UTC+8). Outside that window
   this shim still works but consumes credits.
2. The CLI defaults to "Auto" routing, which may pick a NON-free model.
   You MUST explicitly select Qwen3.8-Flash, or you will burn credits.
   See --model handling below.
3. Terms-of-service: wrapping a vendor CLI into a general-purpose API
   endpoint for third parties is not an explicitly documented use case.
   Using it for your own automation is what `-p` mode is designed for.
   Judge your own risk.
4. The CLI is a subprocess per request. That is SLOW (~seconds of
   startup) and does not stream token-by-token. Fine for personal use,
   wrong for production fan-out.

Requires: Python 3.9+. No third-party packages. Standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
# NOTE: every option here is read at CALL time, not import time, so tests
# and wrappers can override via env without re-importing the module.
# --------------------------------------------------------------------------


def cli_bin() -> str:
    """Path/name of the Qoder CLI binary. Resolved per-call, not at import."""
    return os.environ.get("QODER_CLI_BIN", "qodercn")


# Auth: the CLI reads this env var itself for non-interactive auth.
# Create a PAT at https://qoder.cn/account/integrations
PAT_ENV = "QODERCN_PERSONAL_ACCESS_TOKEN"


def default_model() -> str:
    """Model to request. Defaults to the free one during the promo."""
    return os.environ.get("QODER_DEFAULT_MODEL", "Qwen3.8-Flash")


def listen_host() -> str:
    return os.environ.get("SHIM_HOST", "127.0.0.1")


def listen_port() -> int:
    return int(os.environ.get("SHIM_PORT", "8787"))


def cli_timeout() -> int:
    """Timeout for a single CLI invocation. Agentic tasks can be slow."""
    return int(os.environ.get("QODER_CLI_TIMEOUT", "180"))


def shim_auth_token() -> str:
    """Optional bearer token clients must present, so the shim is not an
    open relay if you ever bind it to 0.0.0.0."""
    return os.environ.get("SHIM_AUTH_TOKEN", "")


# --------------------------------------------------------------------------
# Prompt translation: OpenAI messages[] -> a single prompt string
# --------------------------------------------------------------------------

ROLE_LABEL = {
    "system": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
}


def messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    """
    Flatten an OpenAI-style message array into one prompt string.

    This is where fidelity is LOST. OpenAI's format distinguishes roles,
    tool calls, and multimodal content blocks. The CLI takes a plain
    prompt. We do a best-effort textual rendering.

    A single user message is passed through verbatim -- that is the
    common case and stays clean.
    """
    if not messages:
        return ""

    # Fast path: exactly one user message -> no scaffolding noise.
    if len(messages) == 1 and messages[0].get("role") == "user":
        return content_to_text(messages[0].get("content"))

    parts: list[str] = []
    for m in messages:
        role = ROLE_LABEL.get(m.get("role", "user"), "User")
        text = content_to_text(m.get("content"))
        parts.append(f"{role}: {text}")
    parts.append("Assistant:")
    return "\n\n".join(parts)


def content_to_text(content: Any) -> str:
    """
    Handle both plain-string content and the multimodal list form:
        [{"type":"text","text":"..."},
         {"type":"image_url","image_url":{"url":"..."}}]

    Images cannot be forwarded to a subprocess prompt. We note their
    presence rather than silently dropping them.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                chunks.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                chunks.append(block.get("text", ""))
            elif btype == "image_url":
                chunks.append("[image attachment omitted: CLI shim is text-only]")
            else:
                chunks.append(f"[{btype} block omitted]")
        return "\n".join(chunks)
    return str(content)


# --------------------------------------------------------------------------
# CLI invocation
# --------------------------------------------------------------------------


class CLIError(RuntimeError):
    """Raised when the CLI is missing, times out, or returns junk."""


def build_cli_command(prompt: str, model: str) -> list[str]:
    """
    Construct the argv for a headless CLI call.

    --print / -p            : headless mode, result goes to stdout
    --output-format json    : parseable single JSON object
    Additional per-model flags are vendor-specific and may change; keep
    this the single place you edit when the CLI evolves.
    """
    cmd = [cli_bin(), "-p", prompt, "--output-format", "json"]

    # Only pass a model flag if one was requested. The exact flag name is
    # the most likely thing to break across CLI versions -- verify with
    # `qodercn --help` and adjust here.
    if model:
        cmd += ["--model", model]

    return cmd


def run_cli(prompt: str, model: str) -> dict[str, Any]:
    """Execute the CLI and return its parsed JSON payload."""
    binary = cli_bin()
    if shutil.which(binary) is None:
        raise CLIError(
            f"CLI binary {binary!r} not found on PATH. "
            f"Install it (see README) or set QODER_CLI_BIN."
        )

    env = os.environ.copy()
    if PAT_ENV not in env:
        # Surface the problem loudly rather than letting the CLI hang
        # waiting on an interactive login prompt.
        raise CLIError(
            f"{PAT_ENV} is not set. The CLI would try interactive login "
            f"and block. Export a PAT first."
        )

    cmd = build_cli_command(prompt, model)
    timeout = cli_timeout()
    started = time.time()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=os.environ.get("QODER_WORKDIR", os.getcwd()),
            stdin=subprocess.DEVNULL,  # never let it wait on stdin
        )
    except subprocess.TimeoutExpired as exc:
        raise CLIError(f"CLI timed out after {timeout}s") from exc

    elapsed = time.time() - started

    if proc.returncode != 0:
        raise CLIError(
            f"CLI exited {proc.returncode}\n"
            f"stderr: {proc.stderr.strip()[:2000]}"
        )

    raw = proc.stdout.strip()
    if not raw:
        raise CLIError("CLI produced no stdout")

    # `--output-format json` should give us one JSON object, but be
    # tolerant: find the outermost {...} if there is leading chatter.
    payload = parse_json_loose(raw)
    payload["_shim_elapsed_s"] = round(elapsed, 2)
    return payload


def parse_json_loose(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}
    except json.JSONDecodeError:
        pass

    # Fallback: slice from first '{' to last '}'.
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # Last resort: treat the whole thing as the answer text.
    return {"result": raw}


def extract_text(payload: dict[str, Any]) -> str:
    """
    Pull the assistant's answer out of the CLI's JSON envelope.

    Field names are the second-most-likely thing to change between CLI
    versions. We probe a list of plausible keys, then fall back to
    dumping the whole object so nothing is silently lost.
    """
    for key in ("result", "response", "text", "output", "content", "message"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, dict):
            inner = val.get("content") or val.get("text")
            if isinstance(inner, str) and inner.strip():
                return inner

    # Streaming-style payloads sometimes nest message lists.
    for key in ("messages", "events", "items"):
        seq = payload.get(key)
        if isinstance(seq, list):
            texts = [
                d.get("text") or d.get("content")
                for d in seq
                if isinstance(d, dict)
            ]
            joined = "\n".join(t for t in texts if isinstance(t, str))
            if joined.strip():
                return joined

    return json.dumps(payload, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# OpenAI response shaping
# --------------------------------------------------------------------------


def make_completion_response(text: str, model: str, payload: dict) -> dict:
    """Build a minimal but valid /v1/chat/completions response object."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            # The CLI does not report token counts. Reporting zeros is
            # honest; inventing numbers would be worse.
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        # Non-standard extra, harmless to OpenAI clients, useful for you.
        "_shim": {"cli_elapsed_s": payload.get("_shim_elapsed_s")},
    }


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "QoderShim/1.0"

    # ---- helpers ----

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str, err_type: str = "invalid_request_error") -> None:
        self._send_json(code, {"error": {"message": message, "type": err_type}})

    def _authorized(self) -> bool:
        expected_token = shim_auth_token()
        if not expected_token:
            return True
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {expected_token}"
        return header == expected

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[shim] {self.address_string()} {fmt % args}\n")

    # ---- routes ----

    def do_GET(self) -> None:
        # /v1/models is what OpenAI clients probe first.
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            model = default_model()
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": model,
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "qoder-cli-shim",
                        }
                    ],
                },
            )
            return
        if self.path.rstrip("/") in ("/health", "/"):
            self._send_json(200, {"status": "ok", "cli": cli_bin()})
            return
        self._error(404, f"unknown path {self.path}")

    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._error(404, f"unknown path {self.path}")
            return

        if not self._authorized():
            self._error(401, "invalid shim bearer token", "authentication_error")
            return

        # --- parse body ---
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._error(400, f"malformed JSON body: {exc}")
            return

        messages = req.get("messages")
        if not isinstance(messages, list) or not messages:
            self._error(400, "'messages' must be a non-empty array")
            return

        model = req.get("model") or default_model()

        # --- translate + execute ---
        prompt = messages_to_prompt(messages)
        try:
            payload = run_cli(prompt, model)
        except CLIError as exc:
            self.log_message("CLI failure: %s", exc)
            self._error(502, str(exc), "upstream_error")
            return
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            self.log_message("unexpected failure: %r", exc)
            self._error(500, f"internal shim error: {exc}", "internal_error")
            return

        text = extract_text(payload)

        # --- respond ---
        # Streaming is not implementable on top of a one-shot subprocess,
        # so we always return a single complete response. Clients that
        # asked for stream=true get a well-formed non-streamed body; if
        # that breaks your client, treat it as a hard limitation.
        if req.get("stream"):
            self.log_message("stream=true requested; returning non-streamed")

        self._send_json(200, make_completion_response(text, model, payload))


def main() -> int:
    binary = cli_bin()
    if shutil.which(binary) is None:
        print(
            f"warning: CLI {binary!r} not on PATH. "
            f"Server will start but every request will fail.",
            file=sys.stderr,
        )
    if PAT_ENV not in os.environ:
        print(
            f"warning: {PAT_ENV} not set; requests will fail until you export it.",
            file=sys.stderr,
        )

    host, port = listen_host(), listen_port()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"qoder-shim listening on http://{host}:{port}", file=sys.stderr)
    print(f"  base_url = http://{host}:{port}/v1", file=sys.stderr)
    print(f"  model    = {default_model()}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
