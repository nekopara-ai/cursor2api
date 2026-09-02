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
| `sand/claude-fable-5` | Sand InferenceService/Stream | `claude-fable-5` |
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

The Sand route uses a different RPC, not `AgentService/Run` with a different
header:

```text
POST https://api2.cursor.sh/aiserver.v1.InferenceService/Stream
content-type: application/connect+json
```

Required request properties:

1. 5-byte connect envelope (`0x00` + big-endian length) around JSON;
2. `Authorization: Bearer <session JWT>` (`type: session`, not a web cookie token);
3. `x-cursor-client-type: sand`;
4. `x-cursor-checksum` derived from a XOR-chained millisecond timestamp plus
   machine ids (`CURSOR_MACHINE_ID` / `CURSOR_MAC_MACHINE_ID`, otherwise hashed
   from the session token).

Grok accepts native `tools` and emits `toolCallPart`. Claude models that reject
native tools get an XML prompt and a stream-side parser. Caller tool results are
sent on the next Stream POST (history `toolCalls` / `toolContent`), not MCP.

This is a different protocol from the regular Run stream.

## Verified compatibility surface

| Capability | Sand behavior |
|---|---|
| Streaming and non-streaming text | Supported |
| System text and text-only history | Stream `messages` |
| Caller tools and OpenAI function calling | Supported |
| Structured tool history and tool results | Next Stream POST |
| Stop sequences and output caps | Local approximation |
| Usage fields | Estimated |
| Images, PDFs, files, and audio | Rejected with HTTP 400 |
| JSON Schema and structured output | Rejected with HTTP 400 |
| Multiple choices, logprobs, thinking, reasoning | Rejected with HTTP 400 |

Regular-backend builtin/MCP tools are not used on this path.

## Model-name boundary

The text after `sand/` is sent as `modelId` / `requestedModel`. Upstream may still
remap the model, effort, or reasoning configuration.

## Credentials and trust boundary

Both backends begin with the same authorized Cursor access token. Sand Stream
requires a session-type JWT. A web-type token can pass AuthService but is
rejected on Stream. The adapter:

- keeps the token in the existing credential store;
- does not log the full token;
- sends a new request ID for Stream calls; and
- does not expose upstream credentials to downstream clients.

Endpoint overrides can redirect credential-bearing requests. Use them only with a
trusted test target.

## Cleanup and failure behavior

Sand Stream has no temporary Agent to delete. Failures before response streaming
use the mapped HTTP error status; failures after SSE headers are committed appear
as terminal error events. Missing checksums can surface as `ERROR_OUTDATED_CLIENT`.
Naked JSON without the 5-byte envelope can surface as a protocol envelope error.

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
- Stream checksum / session-token / envelope errors;
- rate or usage limits; and
- transport or proxy failure.

A successful request on one backend is not evidence that the other backend or all
API features are healthy.
