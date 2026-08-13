# cursor2api

[中文说明](README.zh-CN.md)

Use the models of your Cursor account through the Anthropic Messages API and the
OpenAI Chat Completions API.

- `POST /v1/messages` and `POST /v1/chat/completions`, streaming or buffered
- every model the signed-in account can use, listed by `GET /v1/models`
- tools, images, PDFs, thinking/reasoning, usage
- authorise with a Cursor API key or a browser login

## Install

    git clone <your fork>
    cd cursor2api
    pip install -r requirements.txt

Python 3.9+ and the `h2` package.

## Authorise

Either set a key from cursor.com/dashboard:

    export CURSOR_API_KEY=crsr_...

or authorise in the browser:

    python -m cursor2api login

`serve` also does it by itself: with no credentials it prints an authorisation URL
and waits for you to approve it. A server started in the background instead answers
`GET /login` with a URL and picks up the approval on its own:

    curl -s localhost:8787/login

Tokens are kept in `~/.config/cursor2api/credentials.json` (0600). `status` shows what
is in use, `logout` removes it.

## Use

    python -m cursor2api serve            # http://127.0.0.1:8787

Anthropic clients, Claude Code included:

    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_API_KEY=sk-local claude

    curl -s localhost:8787/v1/messages -H 'content-type: application/json' -d '{
      "model": "claude-sonnet-4-5", "max_tokens": 256,
      "messages": [{"role": "user", "content": "hello"}]}'

OpenAI clients, same port:

    OPENAI_BASE_URL=http://127.0.0.1:8787/v1 OPENAI_API_KEY=sk-local

    curl -s localhost:8787/v1/chat/completions -H 'content-type: application/json' -d '{
      "model": "gpt-5.6-sol", "stream": true,
      "messages": [{"role": "user", "content": "hello"}]}'

Routes: `POST /v1/messages`, `POST /v1/messages/count_tokens`,
`POST /v1/chat/completions`, `GET /v1/models`, `GET /v1/models/{id}`, `GET /login`,
`GET /health`.

Common settings: `PORT`, `BIND`, `API_KEY` (require an `x-api-key` from local clients),
`DEFAULT_MODEL`.

## Models

`GET /v1/models` returns the account's own catalog. Any of these spellings works as
`model`:

| form | example |
|---|---|
| base model | `claude-fable-5`, `gpt-5.6-sol`, `gemini-3.1-pro`, `kimi-k3` |
| Cursor variant | `claude-fable-5-thinking-xhigh`, `composer-2.5-fast` |
| alias | `fable`, `sonnet-latest`, `opus`, `codex` |
| explicit parameters | `claude-sonnet-5[thinking=false,effort=max]` |
| another vendor's id | `claude-3-5-sonnet-20241022`, `gpt-4o` (mapped to the nearest model) |

Unknown ids fall back to `DEFAULT_MODEL`.

## Notes

- `temperature`, `top_p`, `top_k`, `cache_control`, `n`, `seed` and `response_format`
  have no equivalent upstream and are ignored; `stop_sequences`, `max_tokens` and
  `tool_choice` are approximated locally.
- Thinking text is Cursor's summary and its Anthropic `signature` is always `""`.
- Web search is Cursor's own server-side tool, reported as `server_tool_use` +
  `web_search_tool_result` with titles and urls only.
- Cursor's agent system prompt is always present and inflates `input_tokens`.
- Rate limits come from the Cursor account and surface as `429` with `retry-after`.
- A model the account has not enabled (some require accepting a data retention
  policy in the Cursor dashboard) answers `403 permission_error`.

Protocol details and field numbers: [docs/protocol.md](docs/protocol.md).

## Tests

Start the server, then:

    python tests/test_api.py
    python tests/test_openai.py

## Disclaimer

This project is an independent, unofficial and experimental tool. It is not affiliated
with, endorsed by or supported by Cursor (Anysphere), Anthropic or OpenAI, and the
names are used only to describe wire formats.

It talks to a private, undocumented and version-gated protocol, so it can stop working
at any time. Using it may conflict with Cursor's terms of service and with the terms of
the model providers behind it; you are responsible for how you use it and for your own
account. No warranty of any kind: see [LICENSE](LICENSE).

## License

MIT.

## Repository topics

    cursor cursor-ai cursor-api anthropic-api claude openai-api openai-compatible
    anthropic-compatible llm-proxy api-proxy reverse-engineering protobuf connect-rpc
    python sse streaming function-calling claude-code
