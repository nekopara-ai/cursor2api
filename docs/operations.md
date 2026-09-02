# Operations

`cursor2api` is designed for a trusted local development environment. This guide
describes the controls the process actually provides and the controls an operator
must supply externally.

## Recommended deployment posture

- Listen on `127.0.0.1` or another explicitly trusted interface.
- Configure `API_KEY` for POST requests.
- Put TLS, network access control, rate limiting, and user isolation in a separate
  reverse proxy or service boundary if remote access is unavoidable.
- Run the process as a dedicated, unprivileged operating-system user.
- Keep the credential store and process environment readable only by that user.
- Leave `SANDBOX_SHELL` disabled unless the entire process is independently
  isolated and every caller is trusted.
- Treat this as an experimental dependency. Pin a known commit and validate it
  after Cursor client or backend changes.

The built-in server does not implement TLS, per-user authentication, tenant
isolation, request quotas, durable queues, distributed state, chunked request-body
decoding, or audit retention.

## Starting the process

Interactive development:

```bash
export CURSOR_API_KEY='crsr_...'
export API_KEY='local-development-key'
python -m cursor2api serve
```

For a service manager, pass the variables directly in the service definition or
load a protected environment file before execution. The application does not load
`.env` itself.

Set `CURSOR2API_AUTO_LOGIN=0` for unattended service startup. Without it, a
missing credential in a non-interactive process starts a background login flow
and still brings up the HTTP listener.

## Health and readiness

### Liveness

`GET /health` returns HTTP 200 when the local HTTP process can answer. It includes
the configured default model but performs no upstream operation.

Use it only for process liveness.

### Readiness

There is no complete readiness endpoint. A practical, read-only check sequence is:

1. confirm `/health` responds;
2. run `python -m cursor2api status` in the same effective environment; and
3. inspect `/v1/models` while remembering that it can fall back to a built-in
   catalog.

Only a real inference request proves the complete path at that moment:

```text
client -> local auth -> request conversion -> token derivation -> network ->
upstream model authorization -> streamed generation -> response conversion
```

Run such probes deliberately because they consume account capacity and can invoke
provider-side data handling.

## Local route exposure

`API_KEY` protects POST handlers only. The following remain unauthenticated in the
current implementation:

- `/health` and `/`;
- `/v1/models` and `/v1/models/{id}`;
- `/login`; and
- every HEAD request.

Consequently, setting `API_KEY` is not sufficient to make a public listener safe.
Network policy should be the primary access boundary.

## Logging

By default, `CURSOR2API_LOG_TURNS=1` emits one JSON object to stderr for each turn.
Fields can include:

- status and timestamps;
- resolved and requested model names;
- client type and selected model parameters;
- numbers of declared tools and returned tool calls;
- whether a live session was resumed or parked;
- stop reason and elapsed milliseconds;
- normalized usage; and
- truncated upstream error text.

Example shape:

```json
{
  "ts": "2026-08-28T12:00:00Z",
  "status": "ok",
  "model": "claude-fable-5",
  "requested": "claude-fable-5",
  "client_type": "cli",
  "tools": 0,
  "tool_calls": 0,
  "resumed": false,
  "parked": false,
  "stop_reason": "end_turn",
  "ms": 1200,
  "usage": {"input_tokens": 10, "output_tokens": 20}
}
```

The logger does not intentionally include complete request bodies or credentials.
Logs can still reveal account activity, model choices, operational errors, and
tool metadata. Protect and rotate them accordingly.

Set `CURSOR2API_LOG_TURNS=0` to disable turn records. Set any non-empty `DBG` value
only for temporary diagnosis; it enables HTTP request logging, selected protocol
events, and local tracebacks.

## Timeouts

The timeout layers cover different failure modes:

| Setting | Default | Meaning |
|---|---:|---|
| `FIRST_TIMEOUT` | 90 s | No meaningful upstream response arrived. |
| `FIRST_OUTPUT_TIMEOUT` | 240 s | Upstream control traffic began, but no user-visible output or terminal event followed. Also applies after tool results are sent. |
| `IDLE_STOP` | 180 s | Stream became silent without a clean terminal event; heartbeats count as activity. |
| Internal hard timeout | 900 s | Absolute limit for one model step. |
| `CURSOR2API_SEND_TIMEOUT` | 120 s | Upstream HTTP/2 flow-control window remained unavailable during a send. |
| `CURSOR2API_HTTP_IDLE` | 300 s | Downstream HTTP/1.1 connection remained idle. |
| `PING_INTERVAL` | 5 s | Quiet period before a downstream SSE keepalive is eligible. |

Timeouts and transport resets after streaming headers appear as errors in the SSE
body while the HTTP status remains 200. Clients must parse terminal events instead
of treating the initial status as proof of success.

## Connection prewarming

The process keeps a small pool of upstream TLS/HTTP/2 connections to reduce time
to first token:

- `CURSOR2API_POOL` controls the target pool size;
- `CURSOR2API_POOL_IDLE` controls the maximum age of an unused connection; and
- the background warmup also refreshes the access-token cache periodically.

Pre-warmed connections are not active inference streams. A Run stream is consumed
by one session and is not returned to the unused pool after the turn.

Set `CURSOR2API_POOL=0` when background outbound connections are undesirable.

## Model catalog behavior

The catalog is account-scoped and cached. Operators should understand three
states:

- **successful catalog**: model and variant information came from the upstream
  AvailableModels RPC;
- **stale successful catalog**: a refresh failed and the previous good catalog
  remains in use; or
- **built-in fallback**: the initial catalog fetch failed, so a static list is
  returned for a shorter retry interval.

`/v1/models` does not label which state produced the response. A model shown in
any state can still fail when called.

## Tool-session state

Live tool sessions are stored only in process memory and expire after
`CURSOR2API_LIVE_TTL`. Operational consequences:

- restarting the process discards parked streams;
- a load-balanced multi-process deployment cannot transparently resume a tool
  stream on another process;
- every parallel tool call in a parked session must be answered together; and
- a follow-up using a different model or client identity uses fresh replay.

If a client waits longer than the TTL before returning results, replay is expected
and not necessarily an error.

## Sand gateway operation

The local `/health` endpoint does not allocate or test a Sand gateway. A Sand
request performs `EnsureSandBox`, creates a temporary Agent, sends one prompt,
polls acceptance and transcript state, and deletes the Agent in cleanup.

Operational consequences:

- unsupported features are rejected with HTTP 400 before an Agent is created;
- an abrupt process or host failure can interrupt cleanup and leave a temporary
  Agent upstream, so inspect the account's Agent list after abnormal shutdown;
- gateway, network, and Agent tokens are ephemeral secrets and must not be logged
  or persisted;
- Sand uses the standard library HTTP client and its normal
  `https_proxy`/`HTTPS_PROXY` behavior; `CURSOR2API_PROXY` configures the
  regular HTTP/2 transport only;
- failures before response streaming use the mapped HTTP error status, while
  failures after SSE headers are committed appear as terminal error events; and
- a successful text probe confirms only the text transcript path, not tools,
  attachments, exact model selection, or structured output.

For a cleanup-sensitive probe, compare the visible Agent set before and after the
request and allow for normal upstream deletion propagation.

## Proxy operation

Set `CURSOR2API_PROXY`, `https_proxy`, or `HTTPS_PROXY` to use an HTTP CONNECT
proxy for the upstream agent connection. `CURSOR2API_PROXY` has priority.

The proxy must permit CONNECT to the Cursor agent host and pass through TLS with
ALPN `h2`. A TLS-intercepting proxy can break certificate verification or HTTP/2
negotiation. Proxy credentials embedded in a URL may be visible through local
process inspection, shell history, or service configuration.

## Failure guide

### `401 authentication_error`

- Run `python -m cursor2api status` with the same environment as the service.
- Check whether an OAuth-only credential expired and requires `login` again.
- Check whether an API key was revoked.
- Confirm that endpoint overrides have not sent the exchange to an unexpected
  host.

The process invalidates its in-memory token after an upstream credential rejection
so the next request can re-derive it.

### `403 permission_error`

The account may not be allowed to use the model, may not have accepted a required
data policy, or the announced client identity/version may no longer be accepted.
Compare with a model returned by the live account catalog and remove experimental
client prefixes before further diagnosis.

### `429 rate_limit_error`

This is an upstream account or model limit. Respect `retry-after` and inspect the
account's official usage and billing controls. Do not infer capacity from this
project's usage estimates or from a model appearing in `/v1/models`.

### `/v1/models` works but inference fails

The catalog may be a built-in fallback, stale, or broader than the current account
authorization. `/health` and model discovery are not end-to-end checks.

### Streaming returns HTTP 200 followed by an error event

The response headers were committed before the upstream failure occurred. Inspect
the SSE body, turn log, timeout configuration, and transport/proxy state.

### Tool continuation replays history

Check whether:

- the complete parallel tool-result set was sent;
- the result IDs exactly match the preceding tool calls;
- model and client prefix are unchanged;
- the TTL expired; or
- the server restarted or lost the upstream stream.

### No response through a proxy

Verify CONNECT success, DNS on the proxy side, certificate trust, and ALPN `h2`.
The Run RPC must remain bidirectional; a generic HTTP bridge that buffers and
half-closes the request is not sufficient.

## Upgrade procedure

Because compatibility depends on private upstream behavior:

1. record the currently deployed commit and effective environment;
2. review protocol, authentication, dependency, and configuration changes;
3. run the complete offline suite;
4. start the candidate on a separate local port;
5. verify liveness, credentials, model catalog, one text request, one streaming
   request, and any tool/image paths your deployment depends on; if Sand is used,
   also run a Sand text probe and confirm the temporary Agent set returns to its
   pre-test state;
6. activate the candidate through the existing supervisor or service manager;
7. verify the new process identity and a real request; and
8. retain a rollback path to the previous commit.

A successful process start proves only that the listener bound; it does not prove
upstream compatibility.
