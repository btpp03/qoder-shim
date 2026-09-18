# Deployment notes

Where this shim can and cannot run. Written from the platform's own
documentation, not assumption — sources are cited so you can re-check.

---

## Requirements

The shim needs exactly two things from a host:

1. **A long-lived process** — it is an HTTP server that must stay up.
2. **A reachable port** — clients connect to it over the network.

Plus, implicitly: the host must be able to **spawn a subprocess** (it shells
out to `qodercn`), and must have **Python 3.9+**.

If a platform can't give you (1) and (2), it can't host this. That is the
whole test.

---

## ❌ Clawdi (`clawdi.ai`) — not possible

**Beware:** `clawdi.com` is an unrelated GoDaddy for-sale parking page.
The real product is `clawdi.ai`.

Clawdi is an **AI-agent hosting/orchestration platform**, not a general PaaS.
It deploys *Agent runtimes*, not arbitrary programs. Three blockers:

### 1. The only deployable artifacts are Hermes or OpenClaw

From [Cloud Agent quickstart](https://docs.clawdi.ai/cloud-agents/deploy):

> Name the Agent and choose **Hermes or OpenClaw**. Choose AI access and
> compute, then continue to review the current price and checkout terms.

There is no Dockerfile path, no buildpack, no `git push` deploy, and no
bring-your-own-image. You cannot deploy "a Python HTTP server" — that
concept does not exist in the product. The docs also warn:

> Do not infer availability if an option is not shown.

### 2. A running foreground process is explicitly NOT durable

From [Agent Interface, Files, and Terminal](https://docs.clawdi.ai/cloud-agents/agent-interface-and-terminal),
the "What persists" table:

| Designed to persist | Not durable |
| --- | --- |
| Supported workspace files | The Terminal connection itself |
| Supported Agent plugins and user configuration | **A running foreground shell process**, temporary files, or runtime scratch data |
| Supported user-level tools | Arbitrary changes to protected operating-system files, packages, or services |

Starting the shim in the browser Terminal lands squarely in the
"not durable" column. It would not survive a restart or a platform update.
Note also that arbitrary **services** (e.g. a hand-written systemd unit) are
in the not-durable column too.

### 3. No mechanism to expose a custom port

Ingress is limited to the runtime's own UI surfaces (Hermes Dashboard,
OpenClaw Control UI). The documentation contains no supported way to map
an arbitrary port to the public internet. Even a live process would be
unreachable from outside.

### What Clawdi *is* for

If the goal is "use a model inside a Clawdi Agent", the shim is the wrong
tool entirely — Clawdi has a first-class **AI Providers** surface for that:

> [Choose AI for your Cloud Agent](https://docs.clawdi.ai/cloud-agents/ai-access)
> — Choose AI access and, when required, a primary model for a Cloud Agent.

But note the catch: an AI Provider expects an **OpenAI-compatible endpoint**,
and Qoder does not publish one (see the main README). So configuring Qoder as
a Clawdi provider fails for the same reason a direct API integration fails.
That is a Qoder-side limitation, not a Clawdi-side one.

### Self-hosting does not help

From [Self-host Clawdi](https://docs.clawdi.ai/self-hosting):

> Self-hosting the open-source service does not include the remote
> infrastructure that Clawdi runs for Cloud Agents.

Self-hosting replaces Clawdi's *control plane*. It gives you no new place to
run a long-lived HTTP server that the public can reach, so it is not a
workaround.

---

## ✅ What does work

Any ordinary Linux host with a persistent process and a bindable port:

- a small VPS (`systemd` unit is the clean approach)
- a container platform that supports long-lived services
- a PaaS with a start command and port binding

The two requirements above are the only checklist. If the host can run
`python3 qoder_shim.py` forever and something can reach the port, it works.

### Rough shape on a VPS

```ini
# /etc/systemd/system/qoder-shim.service
[Unit]
Description=Qoder CLI -> OpenAI-compatible shim
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=qoder
Environment=QODERCN_PERSONAL_ACCESS_TOKEN=pt-xxxx
Environment=QODER_DEFAULT_MODEL=Qwen3.8-Flash
Environment=SHIM_AUTH_TOKEN=change-me
ExecStart=/usr/bin/python3 /opt/qoder-shim/qoder_shim.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Two things that matter in production:

- Put the PAT in an `EnvironmentFile` with `0600` perms rather than inline
  in the unit, or in a secrets manager.
- **Always set `SHIM_AUTH_TOKEN`** if the port is publicly reachable.
  Otherwise anyone who finds the URL gets free compute through your account.
  Better still: bind to localhost and put a real reverse proxy in front —
  ironically, *that* layer is a genuine reverse proxy, which is what this
  repo is not.
