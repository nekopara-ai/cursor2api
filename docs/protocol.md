# Cursor agent protocol notes

Everything here was read off the wire and out of the Cursor CLI's JavaScript bundle.
It is private and undocumented, so treat the field numbers as valid for one CLI
version only. The field lists were regenerated from a locally
installed CLI bundle.

## Transport

    POST https://agentn.global.api5.cursor.sh/agent.v1.AgentService/Run
    HTTP/2, TLS ALPN "h2", bidirectional stream
    content-type: application/connect+proto
    connect-protocol-version: 1
    connect-accept-encoding: gzip
    authorization: Bearer <access token>
    user-agent: connect-es/1.6.1
    x-cursor-client-type: cli | sand
    x-cursor-client-version: cli-<version>   # must be a CLI build, even for sand
    x-sand-box-namespace: prod               # only when client-type is sand
    x-ghost-mode: false
    x-request-id / x-original-request-id: <uuid>

Each message is framed as `[1-byte flags][4-byte big-endian length][protobuf]`,
with `0x01` = gzip payload and `0x02` = end-of-stream frame (a JSON trailer).

A stale `x-cursor-client-version` is rejected with a misleading
`ERROR_GPT_4_VISION_PREVIEW_RATE_LIMIT`, so the value has to track a real CLI release.

## Why the request must be bidirectional

`Run` is bidi-streaming. Immediately after the run request the server sends an
`ExecServerMessage.request_context_args` and waits for the client's
`request_context_result` before it starts the model. A half-closed request (any
buffered HTTP client that sends the whole body first) only ever receives heartbeat
frames. The same applies later in the turn: the server pushes conversation-state
blobs and builtin tool calls through the client and blocks on the answers.
`cursor2api/h2stream.py` therefore drives `h2` over a raw TLS socket.

## Messages

    AgentClientMessage  1 run_request      2 exec_client_message   3 kv_client_message
                        4 conversation_action                      5 exec_client_control
                        6 interaction_response                     7 client_heartbeat
    AgentServerMessage  1 interaction_update   2 exec_server_message
                        3 conversation_checkpoint_update           4 kv_server_message
                        7 interaction_query

    AgentRunRequest     1 conversation_state (required; empty bytes for a new chat)
                        2 action    3 model_details   4 mcp_tools   5 conversation_id
                        8 custom_system_prompt        9 requested_model
                        12 exclude_workspace_context  16 conversation_group_id
                        18 dev_raw_model_slug         19 client_supports_inline_images
                        25 run_id
    UserMessage         1 text  2 message_id  3 selected_context  4 mode
    SelectedContext     1 selected_images   25 selected_documents
    RequestContext      4 env  7 tools  17 web_search_enabled  24 web_fetch_enabled
    InteractionUpdate   1 text_delta  2 tool_call_started  3 tool_call_completed
                        4 thinking_delta  7 partial_tool_call  13 heartbeat
                        14 turn_ended  15 tool_call_delta
    TurnEndedUpdate     1 input_tokens  2 output_tokens  3 cache_read  4 cache_write
                        5 reasoning_tokens

Client-declared tools go into `AgentRunRequest.mcp_tools` as MCP tool definitions;
the model then calls them through `ExecServerMessage.mcp_args`, with Anthropic-style
`toolu_...` identifiers, and the client answers with `mcp_result`.

## Authentication

Two token sources, both ending at the same `Bearer` header:

* `POST https://api2.cursor.sh/auth/exchange_user_api_key` with
  `authorization: Bearer <crsr_ key>` and an empty JSON body returns
  `{accessToken, refreshToken}`. These tokens last about an hour, so the key is
  re-exchanged as needed.
* PKCE browser login: generate a 32-byte verifier, base64url it, hash it with
  SHA-256 for the challenge, then send the user to
  `https://cursor.com/loginDeepControl?challenge=...&uuid=...&mode=login&redirectTarget=cli`
  and poll `GET https://api2.cursor.sh/auth/poll?uuid=...&verifier=...` until it
  returns `{accessToken, refreshToken}` (404 while the user has not confirmed yet).

The CLI has no refresh-token endpoint of its own: when an API key is available it
simply exchanges it again, otherwise the user logs in again.

## Model catalog

The list of models an account may use comes from a plain unary Connect call (no
stream framing, `content-type: application/proto`), the same one the CLI model
picker makes:

    POST https://api2.cursor.sh/aiserver.v1.AiService/AvailableModels

    AvailableModelsRequest   2 include_long_context_models  5 use_model_parameters
                             7 do_not_use_markdown
    AvailableModelsResponse  1 model_names  2 models(AvailableModel)
    AvailableModel           1 name  9 supports_thinking  10 supports_images
                             15 context_token_limit  17 client_display_name
                             29 parameter_definitions  30 variants  35 is_hidden
                             36 legacy_slugs  37 id_aliases  41/42 vendor
    ParameterDefinition      1 id  2 display_name  4 values  5 default_index
    ParameterValues          1 bool_options{1 value,2 label}
                             2 enum_options{1 value,2 label}
    Variant                  1 parameters{1 id,2 value}  4/5 default flags
                             8 client_display_name  9 spec  11 legacy_slug

A model is named on the wire as `RequestedModel{1 name, 3 repeated{1 key,2 value}}`,
where the key/value pairs are exactly the parameter ids from the catalog
(`thinking`, `context`, `effort`, `reasoning`, `fast`, ...). Each variant is one
concrete combination, and its `legacy_slug` is the flat id Cursor uses elsewhere
(`claude-fable-5-thinking-high`, `composer-2.5-fast`). Sending a parameter the model
does not define, or an unknown value, is rejected upstream.

## Server-side system prompt

Cursor injects its own agent system prompt (roughly 24k tokens, which is why
`input_tokens` starts near 25k). Both client-side escapes are refused:
`custom_system_prompt` returns `unknown option '--system-prompt'` and
`exclude_workspace_context = true` returns a permission error. A client `system`
value can steer format and behaviour, but not the assistant's persona.

## Thinking signatures

Not obtainable. `signature` exists only on the client-to-server history types
(`agent.v1.ConversationHistoryReasoningContent{1 text, 2 signature}`,
`aiserver.v1.ConversationMessage.Thinking{1 text, 2 signature, 3 redacted_thinking}`).
Downstream reasoning arrives as `ThinkingDeltaUpdate{text, style}` with no signature
field, and the conversation-state blobs carry the reasoning as plain JSON. The long
base64url string that looks like a signature is
`AgentConversationTurnStructure.encrypted_model`.

Streamed reasoning is also Cursor's short summary of the model's thinking, not a full
chain of thought.

## Web search

Web search and fetch are Cursor's own server-side tools, enabled by
`RequestContext.web_search_enabled` / `web_fetch_enabled`. Results come back inside
`ToolCall.web_search` (query, then title/url/snippet references) and the proxy
converts them to Anthropic `server_tool_use` and `web_search_tool_result` blocks.
`encrypted_content` stays empty and there are no citation blocks.

## Client identity and usage pools

`x-cursor-client-type` selects the meter. `cli` is the plan included/bonus pool.
`sand` is the Grok Bot weekly pool (desktop bundle id `com.anysphere.sand`).
The version header must still name a CLI build; a desktop `0.18.0` on this
`AgentService/Run` stream is `permission_denied`.

Related Dashboard RPCs (unary Connect JSON, empty body, same access token):

    POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandAccessStatus
    POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus

See [usage-pools.md](usage-pools.md).

