# Architecture

`cursor2api` is a protocol translation process, not a model host. It accepts local
HTTP requests, derives a Cursor access token, translates request content into a
private Cursor agent stream, and converts streamed events back into Anthropic- or
OpenAI-shaped responses.

```text
Anthropic/OpenAI client
        |
        | HTTP/1.1 JSON or chunked SSE
        v
cursor2api.server
        |-- request normalization and response rendering
        |-- model resolution and usage normalization
        |-- parked live tool sessions
        |
        v
cursor2api.session
        |-- agent request and protobuf message construction
        |-- caller tool routing and builtin compatibility replies
        |
        v
cursor2api.h2stream
        |-- TLS + ALPN h2
        |-- bidirectional Connect stream
        |-- optional HTTP CONNECT proxy
        v
Cursor agent endpoint
```

## Components

| Module | Responsibility |
|---|---|
| `cursor2api/server.py` | HTTP routes, local API-key gate, Anthropic response shapes, OpenAI facade dispatch, live-session registry, usage normalization, turn logs |
| `cursor2api/openai_api.py` | OpenAI Chat Completions request and response conversion |
| `cursor2api/session.py` | Cursor Run request construction, protobuf field handling, upstream event decoding, tools, attachments, client identity |
| `cursor2api/h2stream.py` | Raw TLS/HTTP/2 transport, flow control, connection prewarming, CONNECT proxy support |
| `cursor2api/auth.py` | Credential precedence, API-key exchange, browser PKCE flow, local credential store |
| `cursor2api/models.py` | Account model catalog, variants, aliases, fallback list, model resolution |
| `cursor2api/pb.py` | Minimal protobuf encoding, decoding, and Connect framing helpers |
| `cursor2api/sandbox.py` | Compatibility replies for selected Cursor builtin file and shell requests |
| `cursor2api/__main__.py` | CLI commands and process startup |

## Request lifecycle

1. `server.Handler` accepts an HTTP request and, for POST routes, checks
   `API_KEY` when configured.
2. The body is parsed using `Content-Length` and normalized to the project's
   Anthropic-style internal shape. OpenAI chat requests pass through
   `openai_api.to_anthropic()`.
3. `Turn` separates an optional client-identity prefix from the model string and
   asks `models.resolve()` for an upstream model plus parameters.
4. The request's message history is flattened. Attachments in the final message
   remain binary context; previous attachments become transcript placeholders.
5. If the final message is exactly a complete set of matching tool results, the
   server attempts to claim a parked live session. Otherwise `Session.start()`
   opens a new upstream stream.
6. `auth.access_token()` selects or derives a usable Cursor bearer token.
7. `h2stream.acquire()` obtains a pre-warmed connection or establishes TLS with
   ALPN `h2`. `Session` starts the bidirectional Connect RPC and sends framed
   protobuf messages without half-closing the request side.
8. Upstream interaction and exec messages are decoded into normalized events:
   text, thinking summaries, web results, tool calls, usage, terminal state, or
   errors.
9. `server.py` renders those events as either buffered JSON or HTTP/1.1 chunked
   SSE in the API flavor selected by the route.
10. A tool-call turn can leave the upstream stream parked. Other terminal paths
    close the session.

## Why the transport is custom

The upstream Run operation is genuinely bidirectional. It can request context,
conversation-state operations, builtin tool results, and MCP tool results while
generation is still in progress. A conventional buffered HTTP client that writes
one request and half-closes the body cannot satisfy this exchange and may receive
only heartbeat frames.

`h2stream.py` therefore owns a raw TLS socket and drives the `h2` state machine
directly. The implementation also:

- preserves the request stream for later tool results;
- acknowledges received flow-controlled data;
- waits for send-window updates with a deadline;
- classifies stream reset and connection termination as errors;
- clears the TCP connection timeout after the TLS handshake; and
- supports an HTTP CONNECT proxy before TLS negotiation.

See [Protocol notes](protocol.md) for the framing and field-level details.

## Authentication boundary

There are two distinct authentication relationships:

```text
local API client -- API_KEY --> cursor2api -- Cursor credential --> Cursor
```

`API_KEY` is an optional shared secret for local POST routes. It is unrelated to
the Cursor account credential. The latter can be an access token, an API key that
is exchanged for a token, or a locally stored browser/CLI credential.

GET and HEAD routes currently bypass the local API-key check. The service does not
provide TLS, user accounts, tenant isolation, audit authorization, or network
policy. Those controls must be supplied outside the process if the listener is
made reachable beyond localhost.

## Model catalog and parameters

`models.py` calls the same account-scoped AvailableModels RPC used by Cursor's
model picker. It records base model IDs, aliases, legacy slugs, variants, supported
parameter names, and parameter values.

Model resolution is intentionally permissive to accommodate existing clients.
Explicit catalog entries are preferred, followed by suffix/parameter parsing and
selected vendor aliases. Failure to resolve returns `DEFAULT_MODEL` rather than a
strict validation error.

Thinking and effort controls are then adapted to parameters actually published by
that model. Some families expose `effort`; others expose `reasoning`; models that
publish neither do not receive an invented parameter.

## Tools and ownership

With the default `CURSOR2API_TOOL_OWNER=caller`:

- caller tool definitions are encoded as MCP tools;
- the upstream allowed-tool header is restricted to MCP control entry points;
- common Cursor builtin tool-name collisions are renamed for transport and mapped
  back for the caller; and
- unknown executable upstream requests are refused so the stream does not wait
  indefinitely.

With `CURSOR2API_TOOL_OWNER=cursor`, the legacy path can answer selected Cursor
builtin operations through `sandbox.py`. That module rewrites paths for its file
operations and refuses shell execution unless `SANDBOX_SHELL=1`. An enabled shell
uses `SANDBOX_ROOT` only as its working directory and is not confined there.

This is a compatibility mechanism, not a security sandbox. It does not create a
container, user namespace, syscall filter, or independent network boundary.

## Live tool sessions

Cursor's stream can remain open while waiting for tool results. Closing it after
every `tool_use` would force the next HTTP request to replay the complete message
history and upstream agent harness. The live-session registry instead parks the
open `Session` for a limited time.

A follow-up resumes the stream only when it contains exactly all tool results
parked for the same session, model, and client identity. This exact-set rule is
important for parallel tool calls: partial continuation would leave the upstream
waiting for results that the API server had already detached.

The registry is in process memory. Restarting the server, exceeding the TTL, or
losing the upstream connection causes the next tool-result request to use the
fresh replay path.

## Streaming and failure semantics

Downstream streaming starts with HTTP 200 because HTTP headers must precede
upstream generation. A later reset, timeout, authentication failure, or other
upstream error is written inside the SSE body. The implementation cannot change
the already-sent status code.

The transport and session layers distinguish a clean terminal event from reset,
GOAWAY, socket failure, first-response timeout, first-visible-output timeout, and
hard timeout. This prevents a truncated answer from being reported as an ordinary
successful end.

## Usage normalization

Cursor's terminal usage frame belongs to its upstream turn, which can span several
HTTP requests when a live tool session is resumed. It is also absent at the moment
a tool-call response is handed to the local API caller.

The server therefore combines real counters with character-based estimates:

- tool-call turns receive estimated values;
- later cumulative values are clamped to the current request's approximate prompt
  size;
- cache counters are bounded by the normalized input total; and
- observed clean turns calibrate a process-local characters-per-token ratio.

The resulting usage is useful for client bookkeeping and diagnostics, but it is
not a tokenizer-exact or billing-authoritative measurement.

## State and concurrency

The HTTP server uses one thread per downstream connection. Shared process state
includes:

- the in-memory access-token cache;
- the model catalog cache;
- the pre-warmed upstream connection pool;
- live tool sessions; and
- a process-local token-estimation calibration value.

These structures do not persist across restarts and are not shared between
multiple cursor2api processes. Running multiple replicas behind a load balancer
can break live tool continuation unless requests are made sticky and the loss of
parked sessions is acceptable.

## Upstream compatibility boundary

The implementation contains field numbers and headers recovered for a particular
Cursor CLI protocol generation. `CURSOR_CLI_VERSION` is therefore part of the wire
contract, not a cosmetic user-agent string. Updating it without revalidating
framing, messages, required client replies, model catalog fields, and runtime
behavior can produce misleading upstream errors or silent incompatibilities.
