# cursor2api

[简体中文](README.zh-CN.md)

`cursor2api` is an experimental local gateway that presents selected parts of
the Anthropic Messages API and OpenAI Chat Completions API in front of a signed-in
Cursor account.

> [!WARNING]
> This is an independent, unofficial project. It is not affiliated with or
> endorsed by Cursor (Anysphere), Anthropic, OpenAI, xAI, or any model provider.
> It depends on a private, undocumented, version-sensitive upstream protocol and
> may stop working without notice. Review the applicable terms for your account
> before using it.

> [!IMPORTANT]
> This repository is fully open source under the MIT License: use, modification,
> redistribution, and commercial use are allowed under the license terms. If the
> project helps you, please [star it on GitHub](https://github.com/nekopara-ai/cursor2api).
> The star is a strongly requested way to support maintenance, not a license
> condition, and the software does not verify stars or collect GitHub credentials.

The project is intended for local development, interoperability experiments,
and protocol research. It is not a drop-in implementation of either vendor API,
and it should not be operated as a public or multi-tenant service.

## What it provides

- Anthropic-style `POST /v1/messages`, including streaming, tools, images,
  documents, and reasoning summaries.
- OpenAI-style `POST /v1/chat/completions`, including streaming and function
  calling.
- Account-scoped model discovery through `GET /v1/models`.
- Cursor API-key exchange, browser login, and optional reuse of Cursor CLI
  credentials.
- A bidirectional HTTP/2 transport for Cursor's agent stream, with optional
  HTTP CONNECT egress.
- Live continuation of tool-call turns when the complete matching tool-result
  set is returned promptly.
- Per-request model-prefix routing between the regular agent stream and the
  narrower, text-only Sand / Grok Bot backend.

## Project boundaries

| Area | Status | Important boundary |
|---|---|---|
| Anthropic Messages | Supported subset | Common message, content, tool, and SSE shapes are translated; this is not full API compatibility. |
| OpenAI Chat Completions | Supported subset | Common chat and function-calling shapes are translated through the Anthropic-style internal representation. |
| `max_tokens` and stop sequences | Approximated | Enforced locally using text matching and an approximate four-characters-per-token cap. |
| Token counts and usage | Approximated | Counts may be estimated or clamped because Cursor does not expose equivalent per-request counters for every tool turn. |
| Sampling and response controls | Accepted with no equivalent behavior | Parameters such as `temperature`, `top_p`, `seed`, penalties, `n`, and `response_format` are not forwarded to an equivalent upstream control. |
| Model aliases | Best effort | Unknown model names fall back to `DEFAULT_MODEL` instead of returning a validation error. |
| Sand / Grok Bot prefixes | Text-only subset | `sand/`, `bot/`, and `grokbot/` use a separate temporary-Agent gateway and explicitly reject tools, attachments, structured output, multiple choices, logprobs, and reasoning blocks. |
| Regular client prefix | Experimental | `cli/` keeps the regular bidirectional AgentService route; upstream acceptance is not guaranteed. |
| Upstream protocol | Unstable | Field numbers and required headers can change with Cursor releases. |

See [API reference](docs/api-reference.md) for the exact request and response
behavior.

## Quick start

Requirements: Python 3.9 or newer, network access to Cursor, and a Cursor account
you are authorized to use.

```bash
git clone https://github.com/nekopara-ai/cursor2api.git
cd cursor2api
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Choose an upstream credential source:

```bash
# Option A: a Cursor API key
export CURSOR_API_KEY='crsr_...'

# Option B: browser authorization stored under ~/.config/cursor2api/
python -m cursor2api login
```

Start a localhost-only server and protect POST requests with a local key:

```bash
export API_KEY='local-development-key'
python -m cursor2api serve
```

The service listens on `http://127.0.0.1:8787` by default. The repository's
[`.env.example`](.env.example) is a reference template only: the application
does **not** load `.env` files automatically. Export variables in the process
environment or load them with your process manager before starting the server.

List the currently available model IDs:

```bash
curl -s http://127.0.0.1:8787/v1/models
```

`GET` and `HEAD` routes are currently not protected by `API_KEY`; keep the
listener on a trusted interface even when a local API key is configured.

## Send a request

Anthropic Messages:

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H 'content-type: application/json' \
  -H 'x-api-key: local-development-key' \
  -d '{
    "model": "claude-fable-5",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

OpenAI Chat Completions:

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer local-development-key' \
  -d '{
    "model": "claude-fable-5",
    "stream": true,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

Replace the example model with an ID returned by `/v1/models`. A successful
catalog response does not prove that a particular model invocation will be
authorized, and the endpoint may return a built-in fallback catalog when the
upstream catalog request fails.

## Sand / Grok Bot mode

Prefix a model with `sand/` (aliases: `bot/`, `grokbot/`) to use the
account's separate Sand / Grok Bot path through the newer in-box gateway:

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H 'content-type: application/json' \
  -H 'x-api-key: local-development-key' \
  -d '{
    "model": "sand/claude-fable-5",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "Hello from Sand"}]
  }'
```

Sand mode preserves the existing Anthropic/OpenAI text response shapes, but it is
**text API compatibility, not full Cursor or vendor API equivalence**. Each normal
request creates a temporary Sand Agent, watches its plain-text transcript, emits
incremental text, and deletes the Agent during cleanup.

| Capability in Sand mode | Status |
|---|---|
| Streaming and non-streaming text | Supported |
| System prompts and text conversation history | Supported by prompt serialization |
| Stop sequences and output limits | Approximated locally |
| Usage fields | Estimated; not billing-authoritative |
| Caller tools / OpenAI function calling | Unsupported; returns `400 invalid_request_error` |
| Structured tool-call history and tool results | Unsupported; returns `400` |
| Images, PDFs, files, and audio | Unsupported; returns `400` |
| JSON Schema / structured output / non-text modalities | Unsupported; returns `400` |
| `n != 1`, logprobs, thinking, and reasoning blocks | Unsupported; returns `400` |
| Exact model selection after the prefix | Not available |

The text after `sand/` is retained as a client-facing routing and response label.
Sand's `sendPrompt` request has no model-selection field, so a name such as
`sand/claude-fable-5` does not guarantee that exact backend or reasoning setting.
Sand's own internal tools are not exposed as standard `tool_use` or `tool_calls`
events. Claude Code, Codex, and agent frameworks that require caller tool-result
round trips are therefore not compatible with this mode.

See [API reference](docs/api-reference.md) and
[client routing](docs/usage-pools.md) for the exact boundary.

## Documentation

| Document | Purpose |
|---|---|
| [Getting started](docs/getting-started.md) | Installation, authentication, CLI commands, and first requests |
| [Configuration](docs/configuration.md) | Complete environment-variable reference and credential precedence |
| [API reference](docs/api-reference.md) | Routes, supported request shapes, streaming, errors, and compatibility limits |
| [Architecture](docs/architecture.md) | Components, request flow, tools, live sessions, and trust boundaries |
| [Operations](docs/operations.md) | Deployment posture, health semantics, logging, timeouts, and troubleshooting |
| [Protocol notes](docs/protocol.md) | Version-sensitive Connect/protobuf transport notes |
| [Client routing and Sand / Grok Bot mode](docs/usage-pools.md) | `cli` and `sand` request routing behavior and risks |
| [Contributing](CONTRIBUTING.md) | Development workflow and pull-request expectations |
| [Security policy](SECURITY.md) | Private reporting and deployment security guidance |

## Security posture

- Keep the default `BIND=127.0.0.1`; the server does not provide TLS.
- Set `API_KEY` for POST routes, but do not treat it as protection for GET or
  HEAD routes.
- Treat `/health` as a process-liveness response only. It does not verify
  credentials, the model catalog, or the upstream inference path.
- Keep credentials outside the repository. The default browser-login store is
  `~/.config/cursor2api/credentials.json` with mode `0600`.
- Do not enable `SANDBOX_SHELL=1` for untrusted traffic. The compatibility
  sandbox is not an operating-system isolation boundary.

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Development

Offline regression tests do not require a Cursor account:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python tests/test_timeouts_offline.py
```

`tests/test_api.py` and `tests/test_openai.py` are live integration scripts.
They require a separately started server, usable credentials, network access,
and account capacity. See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a
change.

## License and trademarks

The code is available under the [MIT License](LICENSE). It may be used,
modified, redistributed, sublicensed, and sold subject to that license. If you
use or benefit from the project, the maintainers strongly ask you to
[star the repository](https://github.com/nekopara-ai/cursor2api); this support
request is not an additional license restriction.

Cursor, Anthropic, OpenAI, xAI, and related product names are trademarks of their
respective owners and are used here only to identify interoperability targets.
