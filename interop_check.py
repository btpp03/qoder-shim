#!/usr/bin/env python3
"""
Interop check: drive the shim with the REAL `openai` SDK.

The unit tests prove our logic; this proves compatibility with a genuine
OpenAI client -- which is the actual requirement ("can my app use it?").

Uses a fake CLI, so no Qoder install or network access is needed.

Run:  pip install openai && python3 interop_check.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAKE_CLI = r'''#!/usr/bin/env python3
import json, sys
argv = sys.argv[1:]
prompt = ""
for i, a in enumerate(argv):
    if a == "-p" and i + 1 < len(argv):
        prompt = argv[i + 1]
model = ""
for i, a in enumerate(argv):
    if a == "--model" and i + 1 < len(argv):
        model = argv[i + 1]
print(json.dumps({
    "result": f"[{model}] reply to: {prompt}",
    "session_id": "interop-1",
}))
'''


def main() -> int:
    try:
        from openai import OpenAI
    except ImportError:
        print("SKIP: `openai` package not installed (pip install openai)")
        return 0

    tmp = tempfile.mkdtemp(prefix="qoder-interop-")
    cli = os.path.join(tmp, "qodercn")
    with open(cli, "w") as fh:
        fh.write(FAKE_CLI)
    os.chmod(cli, os.stat(cli).st_mode | stat.S_IEXEC)

    os.environ["QODER_CLI_BIN"] = cli
    os.environ["QODERCN_PERSONAL_ACCESS_TOKEN"] = "pt-interop-fake"

    import qoder_shim

    server = qoder_shim.ThreadingHTTPServer(("127.0.0.1", 0), qoder_shim.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    failures = []
    base_url = f"http://127.0.0.1:{port}/v1"

    # ---- Test 1: basic completion via real SDK ----
    client = OpenAI(base_url=base_url, api_key="anything")
    print("=" * 66)
    print("TEST 1: client.chat.completions.create() -- basic roundtrip")
    print("=" * 66)
    try:
        resp = client.chat.completions.create(
            model="Qwen3.8-Flash",
            messages=[{"role": "user", "content": "hello from the sdk"}],
        )
        content = resp.choices[0].message.content
        print(f"  id      : {resp.id}")
        print(f"  model   : {resp.model}")
        print(f"  content : {content}")
        assert resp.object == "chat.completion", resp.object
        assert "[Qwen3.8-Flash]" in content, content
        assert "hello from the sdk" in content, content
        assert resp.choices[0].finish_reason == "stop"
        print("  -> PASS")
    except Exception as exc:  # noqa: BLE001
        print(f"  -> FAIL: {type(exc).__name__}: {exc}")
        failures.append("basic")

    # ---- Test 2: multi-turn conversation ----
    print()
    print("=" * 66)
    print("TEST 2: multi-turn messages array")
    print("=" * 66)
    try:
        resp = client.chat.completions.create(
            model="Qwen3.8-Flash",
            messages=[
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "second question"},
            ],
        )
        content = resp.choices[0].message.content
        print(f"  content : {content[:160]}")
        for needle in ("You are terse.", "first question", "second question"):
            assert needle in content, f"missing {needle!r}"
        print("  -> PASS (all turns reached the CLI)")
    except Exception as exc:  # noqa: BLE001
        print(f"  -> FAIL: {type(exc).__name__}: {exc}")
        failures.append("multi-turn")

    # ---- Test 3: client.models.list() ----
    print()
    print("=" * 66)
    print("TEST 3: client.models.list() -- model discovery")
    print("=" * 66)
    try:
        models = client.models.list()
        ids = [m.id for m in models.data]
        print(f"  models  : {ids}")
        assert ids, "no models returned"
        print("  -> PASS")
    except Exception as exc:  # noqa: BLE001
        print(f"  -> FAIL: {type(exc).__name__}: {exc}")
        failures.append("models-list")

    # ---- Test 4: error surfaces properly ----
    print()
    print("=" * 66)
    print("TEST 4: upstream failure surfaces as an API error")
    print("=" * 66)
    try:
        bad = os.path.join(tmp, "broken")
        with open(bad, "w") as fh:
            fh.write("#!/usr/bin/env python3\nimport sys\nsys.exit(9)\n")
        os.chmod(bad, os.stat(bad).st_mode | stat.S_IEXEC)
        os.environ["QODER_CLI_BIN"] = bad
        try:
            client.chat.completions.create(
                model="Qwen3.8-Flash",
                messages=[{"role": "user", "content": "x"}],
            )
            print("  -> FAIL: expected an exception")
            failures.append("error-surface")
        except Exception as exc:  # noqa: BLE001
            print(f"  raised  : {type(exc).__name__}")
            print(f"  message : {str(exc)[:120]}")
            print("  -> PASS (client saw a real error, not a hang)")
    finally:
        os.environ["QODER_CLI_BIN"] = cli

    # ---- Test 5: auth rejection when SHIM_AUTH_TOKEN set ----
    print()
    print("=" * 66)
    print("TEST 5: SHIM_AUTH_TOKEN enforcement")
    print("=" * 66)
    try:
        os.environ["SHIM_AUTH_TOKEN"] = "sekret"
        good = OpenAI(base_url=base_url, api_key="sekret")
        r = good.chat.completions.create(
            model="Qwen3.8-Flash",
            messages=[{"role": "user", "content": "authed"}],
        )
        print(f"  with correct key : {r.choices[0].message.content[:60]}")

        bad_client = OpenAI(base_url=base_url, api_key="wrong")
        try:
            bad_client.chat.completions.create(
                model="Qwen3.8-Flash",
                messages=[{"role": "user", "content": "nope"}],
            )
            print("  -> FAIL: wrong key was accepted")
            failures.append("shim-auth")
        except Exception as exc:  # noqa: BLE001
            print(f"  wrong key rejected: {type(exc).__name__}")
            print("  -> PASS")
    finally:
        os.environ.pop("SHIM_AUTH_TOKEN", None)

    # ---- Test 6: latency reality check ----
    print()
    print("=" * 66)
    print("TEST 6: per-request subprocess overhead")
    print("=" * 66)
    start = time.time()
    for _ in range(3):
        client.chat.completions.create(
            model="Qwen3.8-Flash",
            messages=[{"role": "user", "content": "timing"}],
        )
    elapsed = (time.time() - start) / 3
    print(f"  avg/request (fake CLI): {elapsed*1000:.0f} ms")
    print("  note: a REAL Qoder CLI call adds process startup + model time;")
    print("        expect seconds, not milliseconds.")

    server.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 66)
    if failures:
        print(f"RESULT: {len(failures)} FAILED -> {failures}")
        return 1
    print("RESULT: ALL INTEROP TESTS PASSED")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
