# API reference

`cursor2api` exposes an HTTP/1.1 server with selected Anthropic Messages and
OpenAI Chat Completions shapes. This document describes the implementation in
this repository; it is not a claim of full compatibility with either vendor.

## Authentication to the local server

When `API_KEY` is non-empty, every POST request must provide one of:

```text
x-api-key: <API_KEY>
Authorization: Bearer <API_KEY>
```

The comparison is constant-time. GET and HEAD requests are not currently checked,
including model discovery, health, and login routes.

POST bodies must be JSON objects delivered with `Content-Length`. Chunked request
bodies are not implemented. The server does not attempt full vendor-compatible
schema validation, so malformed top-level or nested values can produce a generic
local/upstream failure instead of a structured field-level error.

## Route summary

| Method | Path | Purpose | Local API key |
|---|---|---|---|
| `POST` | `/v1/messages` | Anthropic-style Messages request | Required when configured |
| `POST` | `/v1/messages/count_tokens` | Approximate input-token count | Required when configured |
| `POST` | `/v1/chat/completions` | OpenAI-style Chat Completions request | Required when configured |
| `POST` | `/chat/completions` | Alias for the OpenAI-style handler | Required when configured |
| `POST` | `/openai/v1/chat/completions` | OpenAI-prefixed alias | Required when configured |
| `GET` | `/v1/models` | Model catalog | Not checked |
| `GET` | `/v1/models/{id}` | One model entry | Not checked |
| `GET` | `/health`, `/`, or empty path | Process liveness | Not checked |
| `GET` | `/login` | Start or inspect background browser authorization | Not checked |
| `HEAD` | any path | Client base-URL probe | Not checked |

No other vendor endpoints are implemented. In particular, the server does not
provide the OpenAI Responses, legacy Completions, Embeddings, Images, Audio, or
Assistants APIs, or Anthropic administrative/batch APIs.

## Anthropic-style Messages

### `POST /v1/messages`

Commonly supported inputs include:

- `model`;
- `messages` with `user` and `assistant` roles;
- string or content-block message bodies;
- `system` as a string or text-block list;
- `stream`;
- base64 image blocks;
- base64 document blocks and text documents;
- `tools`, `tool_choice`, `tool_use`, and `tool_result`;
- `thinking.type=enabled`, `thinking.budget_tokens`, and a reasoning effort;
- `stop_sequences`; and
- `max_tokens`.

`messages` must be present and non-empty. Validation is intentionally limited;
malformed nested structures may surface as a generic 4xx, 5xx, or upstream error
rather than a field-by-field vendor-compatible validation response.

#### Message history

The current implementation starts a fresh Cursor conversation for an ordinary
request. Earlier turns are flattened into a text transcript. Attachments are sent
as binary context only when they occur in the final message; earlier attachments
are represented by textual placeholders.

A tool-result-only final user message can continue a parked live stream when all
of the following match:

- every parked tool call from that stream is answered in the same request;
- the model resolves to the same upstream model;
- the client identity is the same; and
- the parked stream has not exceeded `CURSOR2API_LIVE_TTL`.

Otherwise the complete request is replayed as a fresh conversation.

#### Images and documents

Anthropic-style image blocks require a base64 source. Supported image dimensions
are inferred from PNG, GIF, JPEG, and extended WebP headers; unknown formats use a
fallback size.

Base64 document blocks are forwarded as attachments. Text document sources are
inserted into the prompt as text. Upstream model and account support still decide
whether a particular attachment succeeds.

#### Tools

Tool definitions are translated to the upstream MCP-shaped tool schema. By
default, `CURSOR2API_TOOL_OWNER=caller` exposes caller-declared tools and filters
Cursor's executable builtins. Tool names that collide with Cursor builtin names
are renamed on the wire and mapped back before returning to the API caller.

`tool_choice` is not enforced by an upstream protocol constraint:

- `none` removes the supplied tools;
- `any` adds an instruction requiring one supplied tool;
- a named tool adds an instruction requiring that tool.

The model can still fail to follow the instruction.

#### Thinking

When requested and supported by the resolved model, reasoning summaries are
returned as Anthropic `thinking` blocks. The content is Cursor's streamed summary,
not a full chain of thought. Cursor does not provide an Anthropic-compatible
signature, so `signature` is the empty string.

#### Stop sequences and output limit

Cursor's agent protocol does not expose equivalent controls. The proxy applies
them to emitted text locally:

- the first configured stop sequence found in accumulated text ends output;
- `max_tokens` uses a rough limit of four output characters per token.

This can differ materially from tokenizer-based enforcement, especially for
non-Latin text and structured output.

### Streaming response

With `stream=true`, the server sends HTTP/1.1 chunked server-sent events using
common Anthropic event names:

- `message_start`;
- `content_block_start`;
- `content_block_delta`;
- `content_block_stop`;
- `message_delta`;
- `message_stop`;
- `ping`; and
- `error` after stream headers have already been sent.

Once the HTTP 200 headers are committed, an upstream failure cannot become a new
HTTP status. It is represented as an SSE `error` event in the response body and
the chunked body is terminated.

### Non-streaming response

The response uses an Anthropic-style `message` object with content blocks,
`stop_reason`, `stop_sequence`, and usage. Supported stop reasons include
`end_turn`, `tool_use`, `stop_sequence`, and `max_tokens`.

### `POST /v1/messages/count_tokens`

This endpoint serializes `messages`, adds the extracted system text, and returns:

```json
{"input_tokens": 42}
```

The count is `max(1, characters // 4)`. It does not invoke a model-specific
tokenizer, account for the complete upstream harness, or guarantee parity with
Anthropic's token-counting API.

## OpenAI-style Chat Completions

### Request conversion

The OpenAI-style handler converts the following common structures:

| OpenAI input | Internal conversion |
|---|---|
| `system` and `developer` messages | Combined system text |
| user/assistant text content | Text blocks |
| assistant `tool_calls` | Tool-use blocks |
| `role=tool` | Tool-result blocks |
| `tools[].function` | Anthropic-style tool definitions |
| `tool_choice=none` | No tools |
| `tool_choice=required` | Any supplied tool requested |
| named function choice | Named tool requested |
| `stop` | Stop-sequence list |
| `reasoning_effort` or `reasoning.effort` | Thinking request and nearest supported effort |
| `max_completion_tokens` | Local output cap; wins over `max_tokens` when both are present |

Only base64 `data:` URLs are uploaded as images or files. A remote image URL is
not downloaded; it is converted into a text placeholder containing the URL.

### Responses

Non-streaming responses use `chat.completion`; streaming responses use
`chat.completion.chunk`, followed by `data: [DONE]`. Reasoning summaries are
returned through the non-standard `reasoning_content` field. Tool calls use the
common `tool_calls[].function` shape.

OpenAI finish reasons are mapped as follows:

| Internal stop reason | OpenAI finish reason |
|---|---|
| `end_turn` | `stop` |
| `stop_sequence` | `stop` |
| `max_tokens` | `length` |
| `tool_use` | `tool_calls` |

When a streaming error occurs after HTTP headers, the server emits a `data:` event
containing an `error` object and terminates the chunked response. It does not emit
`[DONE]` after that error.

## Model discovery and resolution

### `GET /v1/models`

Returns a combined model-list shape with fields commonly consumed by both SDK
families. The list is built from the signed-in account's Cursor model-catalog RPC.

If the first upstream fetch fails, a built-in fallback catalog is returned and
cached briefly. If a later refresh fails, the previous successful catalog is
retained. A listed model can still fail at invocation time because of account
permissions, rate limits, data-policy gates, or upstream changes.

### `GET /v1/models/{id}`

Returns one model object only when `{id}` exactly matches an ID in the current
catalog. Otherwise it returns `404 not_found_error`.

### Accepted model spellings

Resolution can recognize:

- base IDs from the catalog;
- published aliases and legacy slugs;
- published variant IDs and specifications;
- compatible suffix tokens such as thinking, effort, reasoning, or fast options;
- explicit `model[key=value,...]` parameters;
- selected Anthropic, OpenAI, Gemini, Grok, and Kimi aliases; and
- experimental client prefixes described in [usage-pools.md](usage-pools.md).

Resolution is permissive. Some vendor-family aliases use prefix matching, and an
unknown ID ultimately falls back to `DEFAULT_MODEL`. Clients that require strict
model validation should compare against `/v1/models` before sending a request.

## Usage reporting

Usage is not always an upstream per-request counter:

- a turn that pauses for a tool call has not yet received Cursor's terminal usage
  frame, so usage is estimated from request and output character counts;
- a resumed live stream may later report counters accumulated across a larger
  upstream turn, so the proxy clamps prompt-side counters to the current request;
- cache-read and cache-creation counters are bounded so they do not exceed the
  reported input total; and
- OpenAI `prompt_tokens`, `completion_tokens`, and `total_tokens` are derived from
  those normalized values.

Treat the fields as operational estimates, not billing-authoritative data.

## Parameters without equivalent upstream behavior

The handlers may accept fields that are not translated to an equivalent Cursor
control. This includes, but is not limited to:

- `temperature`;
- `top_p` and `top_k`;
- `n`;
- `seed`;
- presence and frequency penalties;
- `response_format`;
- prompt/cache-control hints; and
- arbitrary metadata.

Acceptance means the request can proceed; it does not mean the requested behavior
is implemented.

## Errors

Before streaming begins, errors use an Anthropic-style envelope:

```json
{
  "type": "error",
  "error": {
    "type": "invalid_request_error",
    "message": "..."
  }
}
```

Common mappings include:

| Condition | HTTP status | Error type |
|---|---:|---|
| Invalid local API key | `401` | `authentication_error` |
| Missing/invalid JSON or messages | `400` | `invalid_request_error` |
| Unknown route or exact model lookup | `404` | `not_found_error` |
| Cursor rate limit | `429` | `rate_limit_error` |
| Cursor credential rejection | `401` | `authentication_error` |
| Account/model permission rejection | `403` | `permission_error` |
| Model not available for account | `400` | `invalid_request_error` |
| Other upstream or transport failure | `502` | `api_error` |
| Unhandled local exception | `500` | `api_error` |

Streaming clients must inspect the body for an error event even when the initial
HTTP status is 200.

## Health and login routes

### `GET /health`

Returns a small JSON object identifying the process and configured default model.
It does not perform a credential exchange, model call, catalog refresh, or
end-to-end readiness check.

### `GET /login`

Returns `{"authorized": true}` when current credentials are usable. Otherwise it
starts or reports a pending browser flow and returns a `login_url`. The route is
not locally authenticated in the current implementation, so it must remain on a
trusted interface.

### `HEAD *`

Returns `200` with an empty body for client base-URL probes, regardless of path.
