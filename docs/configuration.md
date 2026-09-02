# Configuration

All configuration is read from environment variables at process start. The
application does not automatically load `.env` files. [`.env.example`](../.env.example)
is a reference template, not a configuration loader.

Boolean settings use string values, normally `1` for enabled and `0` for disabled.
Durations are expressed in seconds unless stated otherwise.

## HTTP service

| Variable | Default | Description |
|---|---:|---|
| `BIND` | `127.0.0.1` | Listener address. Keep this on a trusted interface. |
| `PORT` | `8787` | Listener port. |
| `API_KEY` | empty | When set, POST requests must provide the value through `x-api-key` or a bearer `Authorization` header. GET and HEAD routes are not checked. |
| `DEFAULT_MODEL` | `claude-fable-5` | Model used when a request omits `model` or model resolution fails. |

`python -m cursor2api serve --bind`, `--port`, and `--model` set the corresponding
environment values for that process before the server module is loaded.

## Upstream credentials

| Variable | Default | Description |
|---|---:|---|
| `CURSOR_ACCESS_TOKEN` | empty | Short-lived Cursor bearer token. Highest-priority credential source. |
| `CURSOR_API_KEY` | empty | Cursor dashboard API key. Exchanged for a short-lived access token. |
| `CURSOR2API_CREDENTIALS` | `$XDG_CONFIG_HOME/cursor2api/credentials.json` or `~/.config/cursor2api/credentials.json` | Dedicated cursor2api credential store. |
| `CURSOR2API_USE_CLI_AUTH` | `1` | Whether to consider `~/.config/cursor/auth.json` as the final credential source. |
| `CURSOR2API_AUTO_LOGIN` | `1` | Allow `serve` to start a browser/headless login flow when no usable credential is present. Set to `0` to fail startup. |
| `XDG_CONFIG_HOME` | platform environment or `~/.config` | Base directory used when `CURSOR2API_CREDENTIALS` is not set. |

Credential resolution order is:

1. a non-expiring `CURSOR_ACCESS_TOKEN`;
2. `CURSOR_API_KEY`;
3. an access token or API key in `CURSOR2API_CREDENTIALS`;
4. an access token or API key in Cursor CLI's auth file, if enabled.

An access token is treated as expiring when its JWT expiry is missing, invalid, or
within five minutes. API keys are re-exchanged as needed. OAuth-only credentials
cannot be refreshed through a separate CLI refresh endpoint; run `login` again
after they expire.

## Upstream endpoints

These variables primarily exist for testing and controlled compatibility work.
Changing them can send credentials to a different host; do not point them at an
endpoint you do not trust.

| Variable | Default | Description |
|---|---|---|
| `CURSOR_API_BASE_URL` | `https://api2.cursor.sh` | Base URL for API-key exchange and browser-login polling. |
| `CURSOR_WEBSITE_URL` | `https://cursor.com` | Base URL used to build the browser authorization link. |
| `CURSOR_AISERVER_URL` | `https://api2.cursor.sh` | Base URL for the model-catalog RPC. |
| `CURSOR_GROKBOT_URL` | `https://api2.cursor.sh` | Base URL for Sand `InferenceService/Stream`. It receives the Cursor session token. |

The streaming agent host and path are currently fixed in the implementation.

## HTTP/2 transport and proxy

| Variable | Default | Description |
|---|---:|---|
| `CURSOR2API_PROXY` | empty | HTTP CONNECT proxy for the upstream HTTP/2 connection. Takes precedence over standard proxy variables. |
| `https_proxy` | empty | Lowercase fallback proxy variable. |
| `HTTPS_PROXY` | empty | Uppercase fallback proxy variable. |
| `CURSOR2API_POOL` | `2` | Target number of pre-warmed, unused upstream connections. Set to `0` to disable background pool filling. |
| `CURSOR2API_POOL_IDLE` | `40` | Maximum age of a pre-warmed connection before it is discarded. |
| `CURSOR2API_SEND_TIMEOUT` | `120` | Maximum time an upstream send waits for HTTP/2 flow-control capacity. |

The proxy URL may be written with or without a scheme. Basic authentication in
the URL is supported, but putting proxy credentials in process environments can
still expose them to local inspection and process managers.

## Session and client identity

| Variable | Default | Description |
|---|---:|---|
| `CURSOR2API_LIVE_TTL` | `150` | How long an open tool-call stream can remain parked for a matching follow-up request. |
| `CURSOR2API_TOOL_OWNER` | `caller` | `caller` exposes caller-declared tools and filters Cursor builtins; `cursor` enables the legacy builtin-tool path. Aliases `client`/`codex` and `legacy` are accepted. |
| `CURSOR2API_CLIENT_TYPE` | `cli` | Default value of the upstream `x-cursor-client-type` header. |
| `CURSOR_CLI_VERSION` | `cli-2026.08.11-e8db854` | Upstream client-version header used by the agent stream and model-catalog request. It must match a compatible Cursor CLI protocol version. |

Model prefixes override `CURSOR2API_CLIENT_TYPE` per request:

- `sand/`, `bot/`, and `grokbot/` select the Sand Stream backend;
- `cli/` selects the regular bidirectional AgentService backend.

A process-wide `CURSOR2API_CLIENT_TYPE=sand` sends unprefixed requests through
the same Sand backend and capability gate. This routing is experimental. See
[Client routing and Sand mode](usage-pools.md).

## Sand Stream

| Variable | Default | Description |
|---|---:|---|
| `CURSOR_DESKTOP_VERSION` | `3.18.9` | Version string sent on Sand Stream requests. Separate from `CURSOR_CLI_VERSION`. |
| `CURSOR_MACHINE_ID` | derived | Optional checksum machine id. When empty, a stable hash of the session token is used. |
| `CURSOR_MAC_MACHINE_ID` | empty | Optional second machine id concatenated into `x-cursor-checksum`. |

The Sand adapter uses standard `urllib` proxy handling; `CURSOR2API_PROXY` only
configures the regular custom HTTP/2 transport.

## Request behavior

| Variable | Default | Description |
|---|---:|---|
| `CURSOR2API_WEB` | `1` | Enables Cursor server-side web search/fetch only in legacy `CURSOR2API_TOOL_OWNER=cursor` mode. In caller-owned mode, web tools must be supplied by the caller. |
| `CURSOR2API_THINKING` | `auto` | `auto` requests thinking when the request or model name asks for it; `off` disables the proxy's automatic thinking parameter. Other values leave model defaults in place. |

Reasoning effort is mapped to the nearest option published by the resolved model.
Parameter names differ by model family (`effort` or `reasoning`).

## Timeouts and keepalives

| Variable | Default | Description |
|---|---:|---|
| `IDLE_STOP` | `180` | Safety timeout for a stream that becomes silent without a terminal event. Upstream heartbeats count as activity. |
| `FIRST_TIMEOUT` | `90` | Maximum time before any meaningful upstream response is observed. |
| `FIRST_OUTPUT_TIMEOUT` | `240` | Maximum time after upstream control activity begins without user-visible text, reasoning, tools, web results, or a terminal event. Re-armed after tool results are sent. |
| `CURSOR2API_HTTP_IDLE` | `300` | Idle timeout for a downstream HTTP/1.1 connection. |
| `PING_INTERVAL` | `5` | Minimum quiet period before emitting an SSE ping or comment to the downstream client. |

The server also uses an internal 900-second hard limit for one model step. It is
not currently configurable.

## Model catalog

| Variable | Default | Description |
|---|---:|---|
| `MODEL_CACHE_TTL` | `900` | Cache lifetime after a successful account model-catalog fetch. |
| `MODEL_CACHE_FAIL_TTL` | `60` | Retry interval represented by the fallback cache after the first catalog fetch fails. |

If a later refresh fails while a previous catalog exists, the existing catalog is
kept. If the first fetch fails, `/v1/models` returns a built-in fallback list.

## Limits, logging, and debugging

| Variable | Default | Description |
|---|---:|---|
| `CURSOR2API_MAX_BODY` | `67108864` | Maximum request body size in bytes, based on `Content-Length`. |
| `CURSOR2API_LOG_TURNS` | `1` | Emit one JSON record per request turn to stderr. |
| `DBG` | empty | Enable verbose HTTP logging, selected protocol debug events, and tracebacks. Any non-empty value enables it. |

Turn logs can contain model names, parameter selections, tool counts, truncated
upstream error text, timing, and usage estimates. They do not intentionally log
request message bodies, but operational logs should still be treated as sensitive.

## Compatibility sandbox

| Variable | Default | Description |
|---|---:|---|
| `SANDBOX_ROOT` | system temporary directory plus `cursor-sandbox` | Root used for local answers to selected Cursor builtin file/shell requests. |
| `SANDBOX_SHELL` | disabled | Set to `1` to allow shell execution with `SANDBOX_ROOT` as the working directory. The command is not confined to that directory. |
| `CURSOR_WS` | system temporary directory plus `cursor-sandbox` | Workspace path announced to the upstream agent for attachment turns. |

Do not enable shell execution for untrusted clients. A shell command can reference
absolute paths, parent directories, processes, and the network with the permissions
of the cursor2api process. For real isolation, place the entire process in an
independently enforced container or sandbox and apply normal filesystem, network,
and process restrictions.
