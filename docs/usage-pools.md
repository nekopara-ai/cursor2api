# Client routing and Sand / Grok Bot mode

`cursor2api` supports two private Cursor backend paths. Prefixes select a path;
they are not provider names, product entitlements, quota guarantees, or a way to
bypass account policy.

> [!WARNING]
> Use only an account you control. Upstream acceptance, usage accounting, and
> private protocol details can change without notice. Official Cursor account
> surfaces remain authoritative for availability, usage, and billing.

## Prefix routing

| Model string | Backend | Local model label |
|---|---|---|
| `claude-fable-5` | `CURSOR2API_CLIENT_TYPE` (default `cli`) | `claude-fable-5` |
| `cli/claude-fable-5` | Regular bidirectional AgentService route | `claude-fable-5` |
| `sand/claude-fable-5` | Sand / Grok Bot in-box gateway | `claude-fable-5` |
| `bot/claude-fable-5` | Sand alias | `claude-fable-5` |
| `grokbot/claude-fable-5` | Sand alias | `claude-fable-5` |

A process-wide `CURSOR2API_CLIENT_TYPE=sand` sends unprefixed requests through
the Sand backend. Explicit prefixes override the process default.

## Regular `cli` backend

The regular route uses `agent.v1.AgentService/Run`, a bidirectional Connect
stream over HTTP/2. It is the only backend in this repository that can expose the
documented caller-tool, attachment, web-result, and reasoning-summary subsets.

The `CURSOR_CLI_VERSION` header remains part of this private wire contract.
Changing the announced client identity or build string does not grant account
permissions and can make requests fail.

## Sand backend

The Sand route uses the newer account-level control plane and in-box gateway:

1. `EnsureSandBox` returns an ephemeral HTTPS gateway URL and tokens;
2. the adapter validates the gateway host;
3. one temporary Agent is created for the request;
4. system text and text history are serialized into a plain prompt;
5. transcript snapshots are converted into incremental assistant text; and
6. the Agent is deleted during normal cleanup.

This is a different protocol, not the regular Run stream with a different header.

## Verified compatibility surface

| Capability | Sand behavior |
|---|---|
| Streaming and non-streaming text | Supported |
| System text and text-only history | Serialized into the prompt |
| Stop sequences and output caps | Local approximation |
| Usage fields | Estimated |
| Caller tools and OpenAI function calling | Rejected with HTTP 400 |
| Structured tool history and tool results | Rejected with HTTP 400 |
| Images, PDFs, files, and audio | Rejected with HTTP 400 |
| JSON Schema and structured output | Rejected with HTTP 400 |
| Multiple choices, logprobs, thinking, reasoning | Rejected with HTTP 400 |

Sand's own internal tools are not returned as standard `tool_use`,
`tool_calls`, or tool-result events. Tool-dependent clients such as Claude Code,
Codex, and agent frameworks must use the regular backend.

## Model-name boundary

The local server retains the text after `sand/` for client routing and response
compatibility. The gateway's `sendPrompt` request has no model-selection field.
A request named `sand/claude-fable-5` therefore does not prove that Sand selected
that exact model, effort, context, or reasoning configuration.

## Credentials and trust boundary

Both backends begin with the same authorized Cursor access token. The Sand control
plane returns a gateway bearer token and network token. The adapter:

- requires an HTTPS URL whose hostname ends in `.cursorvm.com`;
- keeps gateway credentials in memory;
- sends a new request ID for gateway calls; and
- does not expose those credentials to downstream clients.

Endpoint overrides can redirect credential-bearing requests. Use them only with a
trusted test target.

## Cleanup and failure behavior

Normal completion, local stop limits, downstream disconnects, and request errors
run the session cleanup path and attempt `deleteAgent`. A process crash or forced
termination can prevent that call. After abnormal shutdown, inspect the account's
Agent list before assuming no temporary Agent remains.

Unsupported structured features are rejected before Agent creation. Transport,
authentication, gateway, and account-capacity failures still surface through the
normal local error mapping or, after streaming starts, an SSE error event.

## What routing does not guarantee

- It does not guarantee model availability or continued upstream acceptance.
- It does not prove a quota bucket, reset schedule, included usage, or billing rule.
- It does not implement the xAI API or use an xAI Console key.
- It does not reproduce every Cursor product feature.
- It does not bypass account authorization, data policy, or provider policy.
- It does not make the private gateway stable or supported.

## Diagnosis

Compare the same small text prompt across `cli/` and `sand/` while keeping the
credential, network path, and local server constant. Then separate:

- local capability-gate errors;
- credential rejection;
- account/model permission;
- gateway health and temporary-Agent cleanup;
- rate or usage limits; and
- transport or proxy failure.

A successful request on one backend is not evidence that the other backend or all
API features are healthy.
