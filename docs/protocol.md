# Cursor agent protocol notes

This document records the private upstream behavior currently required by
`cursor2api`. It is an implementation aid, not a public protocol specification.
Field numbers and headers were recovered from observed traffic and a Cursor CLI
bundle and must be treated as version-sensitive.

> [!CAUTION]
> Do not update `CURSOR_CLI_VERSION` merely to make the header look current.
> Revalidate the request schema, required client replies, response events, model
> catalog, authentication flow, and live runtime behavior together.

## Scope

The proxy currently depends on three upstream surfaces:

| Surface | Role |
|---|---|
| `agent.v1.AgentService/Run` | Bidirectional model and tool stream |
| `aiserver.v1.AiService/AvailableModels` | Account model catalog |
| Cursor auth endpoints | API-key exchange and browser-login polling |

The agent host is fixed in `cursor2api/session.py`. Authentication and catalog
base URLs can be overridden for controlled testing; see [Configuration](configuration.md).

## Run transport

```text
POST https://agentn.global.api5.cursor.sh/agent.v1.AgentService/Run
TLS ALPN: h2
RPC: bidirectional Connect stream
Content-Type: application/connect+proto
```

Current request headers include:

```text
authorization: Bearer <Cursor access token>
connect-protocol-version: 1
connect-accept-encoding: gzip
user-agent: connect-es/1.6.1
x-cursor-client-type: cli | sand
x-cursor-client-version: cli-<compatible build id>
x-ghost-mode: false
x-request-id: <uuid>
x-original-request-id: <same uuid>
x-sand-box-namespace: prod          # when client type is sand
x-cursor-agent-allowed-tools: ...  # caller-owned tool mode
```

`x-cursor-client-version` participates in upstream protocol acceptance. A value
from a different product, even when paired with that product's client type, can
be rejected. Misleading upstream error codes are possible when the value is stale
or incompatible.

## Connect framing

Each Connect message uses:

```text
[1 byte flags][4 byte big-endian payload length][payload]
```

Observed flags:

- `0x01`: gzip-compressed protobuf payload;
- `0x02`: end-of-stream envelope whose payload is a JSON trailer.

The trailer may carry a Connect error and must not be classified as a successful
terminal frame merely because the HTTP status was 200.

## Why the request remains open

`Run` is bidirectional. Soon after the run request, the server can send an
`ExecServerMessage.request_context_args` and wait for the corresponding client
reply before generation proceeds. During the same turn it can also request
conversation-state operations, attachment operations, builtin tools, and MCP tool
results.

A buffered client that sends the request and half-closes the body cannot complete
this protocol and may receive only heartbeats. `cursor2api/h2stream.py` therefore
uses a raw TLS socket and drives the HTTP/2 state machine directly.

## Core message envelopes

The following field map reflects the currently implemented generation. Names are
descriptive; only field numbers are carried on the wire.

```text
AgentClientMessage
  1 run_request
  2 exec_client_message
  3 kv_client_message
  4 conversation_action
  5 exec_client_control
  6 interaction_response
  7 client_heartbeat

AgentServerMessage
  1 interaction_update
  2 exec_server_message
  3 conversation_checkpoint_update
  4 kv_server_message
  7 interaction_query

AgentRunRequest
  1 conversation_state
  2 action
  3 model_details
  4 mcp_tools
  5 conversation_id
  8 custom_system_prompt
  9 requested_model
 12 exclude_workspace_context
 16 conversation_group_id
 18 dev_raw_model_slug
 19 client_supports_inline_images
 25 run_id

UserMessage
  1 text
  2 message_id
  3 selected_context
  4 mode

SelectedContext
  1 selected_images
 25 selected_documents

RequestContext
  4 env
  7 tools
 17 web_search_enabled
 24 web_fetch_enabled

InteractionUpdate
  1 text_delta
  2 tool_call_started
  3 tool_call_completed
  4 thinking_delta
  7 partial_tool_call
 13 heartbeat
 14 turn_ended
 15 tool_call_delta

TurnEndedUpdate
  1 input_tokens
  2 output_tokens
  3 cache_read
  4 cache_write
  5 reasoning_tokens
```

The implementation intentionally preserves undecoded or newly observed fields as
debug signals instead of treating every unknown field as fatal. Executable exec
requests still need a refusal or result; ignoring them can leave the upstream
stream waiting indefinitely.

## Run request construction

A new request currently sends an empty required conversation-state field, a user
message action, generated conversation and run identifiers, inline-attachment
support, requested model parameters, and caller tool definitions when present.

The upstream options represented by `custom_system_prompt` and
`exclude_workspace_context` have not been usable in the observed protocol. The
local API system text is therefore prepended to the user turn inside a `<system>`
wrapper. This changes prompting behavior but does not replace Cursor's own injected
agent harness.

## Attachments and context

Selected images carry data, generated IDs, a path label, width, height, and MIME
type. Selected documents carry data, a filename/path label, and MIME type.

The proxy announces a compatibility workspace only for attachment turns because
upstream attachment handling can send builtin file operations back to the client.
Ordinary chat turns avoid announcing a directory so the agent is less likely to
explore a workspace the API caller did not provide.

## Caller-declared tools

Caller tool definitions are encoded as MCP tools:

```text
McpTools
  1 repeated McpToolDefinition

McpToolDefinition
  1 name
  2 description
  4 provider_identifier
  5 tool_name
  6 input_schema_json

McpArgs
  1 name
  2 args map
  3 tool_call_id
  4 provider_identifier
  5 tool_name
  8 skip_approval

McpResult
  1 success
```

MCP argument values use protobuf `Value`-like variants. The decoder must preserve:

- null;
- booleans;
- strings;
- numbers, with integral doubles restored as integers;
- recursive `struct_value` maps; and
- recursive `list_value` arrays.

Treating nested values as UTF-8 strings corrupts structured tool arguments. Tool
names that collide with Cursor builtins are changed for the upstream transport and
mapped back before delivery to the API caller.

## Model request

The model is sent as a requested model ID plus repeated string key/value
parameters:

```text
RequestedModel
  1 model ID
  3 repeated parameter { 1 key, 2 value }
```

The accepted parameter IDs and values come from the account catalog. They differ
by family: for example, one model may publish `effort`, another `reasoning`, and a
third neither. Inventing an unsupported parameter can cause an upstream rejection.

## Model catalog RPC

```text
POST https://api2.cursor.sh/aiserver.v1.AiService/AvailableModels
content-type: application/proto
connect-protocol-version: 1
```

This is a unary Connect request rather than a framed bidirectional stream.

```text
AvailableModelsRequest
  2 include_long_context_models
  5 use_model_parameters
  7 do_not_use_markdown

AvailableModelsResponse
  1 model_names
  2 repeated AvailableModel

AvailableModel
  1 name
  9 supports_thinking
 10 supports_images
 15 context_token_limit
 17 client_display_name
 29 parameter_definitions
 30 variants
 35 is_hidden
 36 legacy_slugs
 37 id_aliases
 41/42 vendor fields observed in some generations

ParameterDefinition
  1 id
  2 display_name
  4 values
  5 default_index

Variant
  1 repeated parameter { 1 id, 2 value }
  4/5 default flags
  8 client_display_name in some generations
  9 specification
 11 legacy_slug
```

The proxy uses variants, aliases, legacy slugs, and parameter definitions for
model resolution. It falls back to a built-in catalog when the first fetch fails;
see [API reference](api-reference.md#model-discovery-and-resolution).

## Authentication protocol

### API-key exchange

```text
POST <CURSOR_API_BASE_URL>/auth/exchange_user_api_key
authorization: Bearer <Cursor API key>
content-type: application/json
body: {}
```

The expected response contains an `accessToken` and may contain a `refreshToken`.
The implementation re-exchanges the API key when the access token is expiring.

### Browser authorization

The browser flow generates a random verifier, derives a SHA-256 PKCE challenge,
and sends the user to a Cursor login URL containing the challenge, flow UUID,
`mode=login`, and `redirectTarget=cli`. The client polls:

```text
GET <CURSOR_API_BASE_URL>/auth/poll?uuid=<uuid>&verifier=<verifier>
```

HTTP 404 means authorization has not completed yet. The implementation has no
separate refresh-token call for OAuth-only CLI credentials; an expired session
requires another browser login.

## Thinking data

Reasoning arrives through a thinking-delta update with text and style, but no
Anthropic-compatible signature. Conversation-state data may contain other opaque
or encrypted fields, but those are not a usable downstream signature.

The emitted reasoning is Cursor's summary stream, not full hidden reasoning.

## Web results

In legacy Cursor-owned tool mode, request-context flags can enable upstream web
search and fetch. Observed web tool results contain a query and reference entries
such as title, URL, and snippet. The Anthropic facade renders server-tool/result
blocks; the OpenAI facade renders readable text lines.

There is no equivalent Anthropic encrypted-content value or complete citation
object in the observed stream, so those fields cannot be reconstructed.

## Client identity

The `x-cursor-client-type` header is distinct from the CLI-version header and can
be overridden per request through model prefixes. This path is experimental and
is documented separately in [Experimental client identity routing](usage-pools.md).

Do not infer billing, eligibility, or product entitlement from the header alone.
Those are upstream account decisions outside this project's contract.
