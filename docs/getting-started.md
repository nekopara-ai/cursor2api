# Getting started

This guide covers a local installation, upstream authentication, the command-line
interface, and the first Anthropic- and OpenAI-style requests.

## Before you begin

You need:

- Python 3.9 or newer;
- network access to Cursor's services;
- a Cursor account and credentials you are authorized to use; and
- a trusted local machine or private development environment.

`cursor2api` uses a private upstream protocol. Compatibility can change without a
new release of this repository, and use of the proxy may be subject to terms set
by Cursor and the model providers behind the account.

## Install from source

```bash
git clone https://github.com/nekopara-ai/cursor2api.git
cd cursor2api
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

The only direct runtime dependency is `h2`. An editable installation also exposes
the `cursor2api` console command; `python -m cursor2api` works without relying on
that entry point.

## Configure authentication

The proxy ultimately needs a short-lived Cursor access token. The following
sources are tried in order:

1. `CURSOR_ACCESS_TOKEN`;
2. `CURSOR_API_KEY`;
3. a token or API key in `CURSOR2API_CREDENTIALS`;
4. Cursor CLI's `~/.config/cursor/auth.json`, unless
   `CURSOR2API_USE_CLI_AUTH=0`.

### Use a Cursor API key

```bash
export CURSOR_API_KEY='crsr_...'
python -m cursor2api status
```

The API key is exchanged for a short-lived access token and may be stored in the
configured cursor2api credential file so it can be exchanged again later.

### Use browser authorization

```bash
python -m cursor2api login
```

For a terminal where a browser cannot be opened automatically:

```bash
python -m cursor2api login --no-browser
```

The command prints a URL and waits for the authorization to complete. The default
credential path is `~/.config/cursor2api/credentials.json`; the file is written
with mode `0600`.

### Reuse Cursor CLI credentials

If `~/.config/cursor/auth.json` exists, it is considered after the dedicated
cursor2api credential store. Disable this fallback when credential isolation is
required:

```bash
export CURSOR2API_USE_CLI_AUTH=0
```

### Inspect or remove stored credentials

```bash
python -m cursor2api status
python -m cursor2api logout
```

`logout` removes only the cursor2api credential store. It does not modify the
Cursor CLI authentication file or environment variables.

## Start the HTTP server

```bash
export API_KEY='local-development-key'
python -m cursor2api serve
```

Default address: `http://127.0.0.1:8787`.

Command-line overrides are available for the main listener settings:

```bash
python -m cursor2api serve \
  --bind 127.0.0.1 \
  --port 8787 \
  --model claude-fable-5
```

Equivalent environment variables are `BIND`, `PORT`, and `DEFAULT_MODEL`.

The server can start without usable credentials when automatic login is enabled.
In a non-interactive environment it prints an authorization URL and exposes the
same flow through `GET /login`. Set `CURSOR2API_AUTO_LOGIN=0` to fail startup
instead.

The application does not parse `.env` files. If you copy `.env.example` to
`.env`, load it explicitly in the shell or with your service manager before
starting the process.

## Verify the process

```bash
curl -s http://127.0.0.1:8787/health
curl -s http://127.0.0.1:8787/v1/models
```

`/health` is a liveness response only. `/v1/models` can return a built-in fallback
catalog when the upstream catalog is unavailable. Neither check proves that an
inference request will succeed.

GET and HEAD routes are not currently gated by `API_KEY`. Do not expose the
listener to an untrusted network.

## Call the Anthropic-style API

Non-streaming request:

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H 'content-type: application/json' \
  -H 'x-api-key: local-development-key' \
  -d '{
    "model": "claude-fable-5",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "Explain HTTP/2 in one sentence."}]
  }'
```

Set `"stream": true` to receive Anthropic-style server-sent events.

## Call the OpenAI-style API

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer local-development-key' \
  -d '{
    "model": "claude-fable-5",
    "stream": true,
    "messages": [{"role": "user", "content": "Explain HTTP/2 in one sentence."}]
  }'
```

The following paths reach the same OpenAI-style handler:

- `/v1/chat/completions`
- `/chat/completions`
- `/openai/v1/chat/completions`

## Configure an existing client

For clients that support vendor base-URL overrides, point them at the local
listener and use the configured local `API_KEY` as the client key:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_API_KEY=local-development-key

export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
export OPENAI_API_KEY=local-development-key
```

Client libraries vary in which endpoints and optional fields they call. Consult
[API reference](api-reference.md) before assuming a client is fully compatible.

## Use the one-shot raw CLI

The `chat` command calls the upstream protocol directly instead of going through
the local HTTP facade:

```bash
python -m cursor2api chat 'Say hello in Japanese.'
python -m cursor2api chat 'Solve 17 * 23.' \
  --model claude-fable-5 \
  --idle 12 \
  --thinking
```

`--idle` is the number of silent seconds tolerated by this one-shot command.
`--thinking` prints upstream reasoning summaries to stderr.

## Next steps

- Read [Configuration](configuration.md) before changing listener, proxy,
  timeout, model-cache, or sandbox settings.
- Read [API reference](api-reference.md) before connecting an existing SDK.
- Read [Operations](operations.md) before running the process unattended.
- Read [Client routing and Sand / Grok Bot mode](usage-pools.md) before using a
  model prefix such as `sand/`.
