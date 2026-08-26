# Agent instructions

This file is for coding agents (Cursor, Claude Code, Codex, Copilot, etc.)
working in this repository. Humans should read [README.md](README.md) first.

## What this repo is

An unofficial Python proxy: Anthropic `/v1/messages` + OpenAI `/v1/chat/completions`
in front of Cursor's private bidirectional Connect RPC
`agent.v1.AgentService/Run` on `agentn.global.api5.cursor.sh`.

## Non-negotiables

- **Never commit secrets.** No `credentials.json`, `.env`, cookies, JWTs,
  `crsr_` keys, proxy passwords, account emails, user ids, or server logs.
  Tokens live in `~/.config/cursor2api/` (0600) or env vars. `.env.example` stays
  commented placeholders only.
- **Do not bind `0.0.0.0` in examples or defaults.** Default is `127.0.0.1`.
- **Do not add exploit PoCs, malware, or account-farming / token-pool code.**
  This project is a personal-account proxy, not a marketplace.
- **Do not rewrite git history** unless the user explicitly asks.
- Keep the MIT license in [LICENSE](LICENSE).

## Layout

| path | role |
|---|---|
| `cursor2api/server.py` | HTTP facade, live tool sessions, usage estimates |
| `cursor2api/session.py` | Run RPC, protobuf, `x-cursor-client-type` |
| `cursor2api/h2stream.py` | HTTP/2 + optional HTTP CONNECT |
| `cursor2api/auth.py` | API-key exchange / PKCE / CLI auth file |
| `cursor2api/models.py` | `AvailableModels` catalog and aliases |
| `cursor2api/openai_api.py` | OpenAI request/response mapping |
| `cursor2api/pb.py` | tiny protobuf encode/decode |
| `cursor2api/sandbox.py` | local answers to Cursor builtin file/shell tools |
| `docs/protocol.md` | wire format and field numbers |
| `docs/usage-pools.md` | `cli` vs `sand` metering |
| `tests/` | live HTTP tests (need a running server + account) |

## Protocol constraints you must not "simplify"

1. **Run is bidi-streaming HTTP/2.** A buffered client that sends the whole body
   and half-closes only ever sees heartbeats. Keep `h2stream.py`.
2. **`x-cursor-client-version` must be a CLI build id**
   (`cli-YYYY.MM.DD-……`). Putting Grok Bot's `0.18.0` on this stream is
   `permission_denied`. Version validates the *transport*; `x-cursor-client-type`
   selects the *meter*.
3. **Usage pool = `x-cursor-client-type`.** `cli` → plan included/bonus.
   `sand` → Grok Bot weekly pool. Same access token. See
   `split_client_type()` in `session.py`.
4. **Parked live sessions must match both `model` and `client_type`.** Mixing
   pools on one Run stream is wrong.
5. Cursor injects a large agent harness; you cannot turn it off
   (`custom_system_prompt` / `exclude_workspace_context` are refused).
6. Field numbers in `session.py` / `docs/protocol.md` come from a specific CLI
   bundle. Bump `CURSOR_CLI_VERSION` only together with a protocol re-read.

## When changing behaviour

- Keep public HTTP shapes (Anthropic + OpenAI) stable unless the README is
  updated in the same change.
- New env vars go in `.env.example` and the README configuration table.
- After touching `session.py` / `server.py` / `h2stream.py`, import checks
  belong in `cursor2api.session` (`split_client_type`), and a localhost
  `/v1/messages` call if credentials are available.
- Do not add network calls to xAI `api.x.ai` for Grok Bot. That is a different
  product and a different wallet. Sand metering stays on Cursor's aiserver.

## Style

- Python 3.9+, stdlib + `h2` only unless a dependency is discussed first.
- Match the existing compact style in `server.py` / `session.py`; do not
  reformat the whole file in an unrelated PR.
- Comments in English. User-facing README keeps both English and Chinese.

## What not to "fix"

- Empty Anthropic thinking `signature` — Cursor does not send one.
- Harness-inflated `input_tokens` — that is Cursor, not a leak in this proxy.
- Builtin Cursor tools colliding with client tools — already remapped via
  `wire_names` / `BUILTIN_TOOLS`.
