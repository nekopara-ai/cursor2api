# cursor2api

[中文说明](README.zh-CN.md)

Use the models on a signed-in Cursor account through the Anthropic Messages API
and the OpenAI Chat Completions API.

An unofficial proxy in front of Cursor's agent protocol, with HTTP CONNECT
egress, usage estimates for unfinished tool turns, live tool-call session
resume, and a **Grok Bot (`sand`) usage-pool switch**.

- `POST /v1/messages` and `POST /v1/chat/completions`, streaming or buffered
- every model the signed-in account can use, listed by `GET /v1/models`
- tools, images, PDFs, thinking/reasoning, usage
- authorise with a Cursor API key or a browser login
- optional `sand/` / `bot/` model prefix to draw on the Grok Bot weekly pool

## Disclaimer

This project is an independent, unofficial and experimental tool. It is not
affiliated with, endorsed by or supported by Cursor (Anysphere), Anthropic,
OpenAI or xAI. Product names are used only to describe wire formats.

It talks to a **private, undocumented, version-gated protocol**. Cursor can
change the protocol, tighten client-identity checks, or suspend accounts at any
time. Using it may conflict with Cursor's terms of service and with the terms of
the model providers behind it. You are responsible for how you use it and for
your own account. No warranty of any kind: see [LICENSE](LICENSE).

**Do not expose this proxy to the public internet.** Bind to localhost, set
`API_KEY`, and never commit credentials.

## Install

```bash
git clone https://github.com/nekopara-ai/cursor2api.git
cd cursor2api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.9+ and the `h2` package.

## Authorise

Pick one:

```bash
# 1. API key from https://cursor.com/dashboard
export CURSOR_API_KEY=crsr_...

# 2. Browser PKCE login (writes ~/.config/cursor2api/credentials.json, mode 0600)
python -m cursor2api login

# 3. Reuse a Cursor CLI session (~/.config/cursor/auth.json)
#    enabled by default via CURSOR2API_USE_CLI_AUTH=1
```

`serve` can also start a login itself: with no credentials it prints an
authorisation URL. A background server answers `GET /login` with that URL and
picks up the approval on its own.

```bash
python -m cursor2api status    # which credential source is in use
python -m cursor2api logout    # delete the stored file
```

## Use

```bash
python -m cursor2api serve            # http://127.0.0.1:8787
```

Anthropic clients, Claude Code included:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_API_KEY=sk-local

curl -s localhost:8787/v1/messages -H 'content-type: application/json' -d '{
  "model": "claude-sonnet-4-5", "max_tokens": 256,
  "messages": [{"role": "user", "content": "hello"}]}'
```

OpenAI clients, same port:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
export OPENAI_API_KEY=sk-local

curl -s localhost:8787/v1/chat/completions -H 'content-type: application/json' -d '{
  "model": "gpt-5.6-sol", "stream": true,
  "messages": [{"role": "user", "content": "hello"}]}'
```

Routes: `POST /v1/messages`, `POST /v1/messages/count_tokens`,
`POST /v1/chat/completions`, `GET /v1/models`, `GET /v1/models/{id}`,
`GET /login`, `GET /health`.

## Usage pools: `cli` vs `sand` (Grok Bot)

Cursor meters usage from the **announced client identity**, not from the model
id and not from which hostname you hit.

| `x-cursor-client-type` | What it is | Meter |
|---|---|---|
| `cli` (default) | Cursor CLI / this proxy's original identity | Plan included + bonus pool |
| `sand` | Grok Bot desktop (`com.anysphere.sand`) | Independent Grok Bot weekly pool |

Same Cursor access token, same `agent.v1.AgentService/Run` stream. Only the
client-type header changes.

Prefix the model name **per request**:

```bash
# Plan pool (default)
{"model": "claude-opus-5", ...}

# Grok Bot weekly pool — works even when the plan API pool is exhausted
{"model": "sand/claude-opus-5", ...}
{"model": "bot/gpt-5.2", ...}
{"model": "grokbot/composer-2.5", ...}

# Force the plan pool even if CURSOR2API_CLIENT_TYPE=sand
{"model": "cli/grok-4.6", ...}
```

Or set a process-wide default:

```bash
export CURSOR2API_CLIENT_TYPE=sand
```

**Hard constraint:** `x-cursor-client-version` must remain a **Cursor CLI**
build id (default `cli-2026.08.11-e8db854`, override with `CURSOR_CLI_VERSION`).
Sending Grok Bot's desktop version (`0.18.0`) on this stream returns
`permission_denied`. The server uses the version header to validate the
transport, and the client-type header to pick the meter.

Grok Bot access (as of 2026-08-26) is granted to Cursor Pro / Pro+ / Ultra and
matching SuperGrok plans. Check your own account:

```bash
# empty JSON body, Connect + JSON
curl -s -X POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandAccessStatus \
  -H "authorization: Bearer $CURSOR_ACCESS_TOKEN" \
  -H "content-type: application/json" \
  -H "connect-protocol-version: 1" \
  -H "x-cursor-client-type: sand" \
  -d '{}'

curl -s -X POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus \
  -H "authorization: Bearer $CURSOR_ACCESS_TOKEN" \
  -H "content-type: application/json" \
  -H "connect-protocol-version: 1" \
  -H "x-cursor-client-type: sand" \
  -d '{}'
```

`GetSandAccessStatus` should report `SAND_ACCESS_STATE_GRANTED`.
`GetSandUsageStatus` reports `usagePercent`, `nextResetTimestampUtc` (weekly)
and `grokPlanLabel`. On-demand billing may be eligible after the included pool
is empty — check the Cursor spending dashboard before you rely on this.

This is unofficial client-identity switching. If Cursor starts tying `sand` to a
real machine checksum or to a matching desktop version, the prefix will stop
working.

Details: [docs/usage-pools.md](docs/usage-pools.md).

## Models

`GET /v1/models` returns the account's own catalog. Any of these spellings works
as `model`:

| form | example |
|---|---|
| base model | `claude-fable-5`, `gpt-5.6-sol`, `gemini-3.1-pro`, `kimi-k3` |
| Cursor variant | `claude-fable-5-thinking-xhigh`, `composer-2.5-fast` |
| alias | `fable`, `sonnet-latest`, `opus`, `codex` |
| explicit parameters | `claude-sonnet-5[thinking=false,effort=max]` |
| another vendor's id | `claude-3-5-sonnet-20241022`, `gpt-4o` (mapped to the nearest model) |
| usage-pool prefix | `sand/claude-opus-5`, `bot/grok-4.6`, `cli/composer-2.5` |

Unknown ids fall back to `DEFAULT_MODEL`.

## Configuration

See [.env.example](.env.example). Common variables:

| variable | default | meaning |
|---|---|---|
| `BIND` / `PORT` | `127.0.0.1` / `8787` | listen address |
| `API_KEY` | empty | require `x-api-key` / bearer from local clients |
| `DEFAULT_MODEL` | `claude-fable-5` | fallback model |
| `CURSOR_API_KEY` | — | `crsr_...` dashboard key |
| `CURSOR2API_CREDENTIALS` | `~/.config/cursor2api/credentials.json` | OAuth store |
| `CURSOR2API_CLIENT_TYPE` | `cli` | default usage pool |
| `CURSOR_CLI_VERSION` | `cli-2026.08.11-e8db854` | `x-cursor-client-version` |
| `CURSOR2API_PROXY` / `https_proxy` | — | HTTP CONNECT for the HTTP/2 stream |
| `CURSOR2API_LIVE_TTL` | `150` | seconds a parked tool-call stream is kept |
| `CURSOR2API_WEB` | `1` | enable Cursor server-side web search/fetch |
| `CURSOR2API_THINKING` | `auto` | when to request reasoning |

## Architecture (short)

```
client (Anthropic / OpenAI JSON)
        │  HTTP/1.1  :8787
        ▼
cursor2api.server  ── live tool sessions (_live_sessions)
        │  Connect+protobuf, HTTP/2
        ▼
agentn.global.api5.cursor.sh  /agent.v1.AgentService/Run
```

- `h2stream.py` — bidirectional HTTP/2 (the Run RPC blocks if the request is
  half-closed). Optional HTTP CONNECT via `CURSOR2API_PROXY`.
- `session.py` — protobuf field numbers, client-type header, tool/sandbox loop.
- `server.py` — Anthropic + OpenAI facades, usage estimates when `turn_ended`
  has not arrived yet, live resume of tool_use turns.
- `auth.py` — API-key exchange or PKCE; tokens are never written into the repo.
- `models.py` — `AvailableModels` catalog + aliases.

Protocol field numbers: [docs/protocol.md](docs/protocol.md).

## Notes and known limits

- `temperature`, `top_p`, `top_k`, `cache_control`, `n`, `seed` and
  `response_format` have no upstream equivalent and are ignored.
- Thinking text is Cursor's summary; Anthropic `signature` is always `""`.
- Web search is Cursor's own server-side tool (`server_tool_use` + titles/urls).
- Each **new** session carries Cursor's agent harness (~12k–25k input tokens).
  Live tool-session resume avoids replaying that harness on the next tool turn.
- Tiny images (e.g. 16×16) may be rejected with 429; normal screenshots work.
- Rate limits are the account's, returned as `429` + `retry-after`.
- A model the account has not enabled answers `403 permission_error`.

## Tests

Start the server, then:

```bash
python tests/test_api.py
python tests/test_openai.py
```

These hit a live Cursor account. They will fail if the plan pool is exhausted
unless you point `MODEL` at a `sand/...` id that your account can use.

## License

MIT. See [LICENSE](LICENSE).
