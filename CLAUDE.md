# Claude Code / agent entry

Read [AGENTS.md](AGENTS.md) before editing. Summary:

- Unofficial Cursor agent-protocol proxy (Anthropic + OpenAI facades).
- Never commit credentials, `.env`, JWTs, or proxy passwords.
- `x-cursor-client-type` selects the usage pool (`cli` vs `sand`).
- `x-cursor-client-version` must stay a Cursor **CLI** build id.
- Run RPC is bidirectional HTTP/2; do not replace `h2stream.py` with requests.

Chinese README: [README.zh-CN.md](README.zh-CN.md).
Protocol: [docs/protocol.md](docs/protocol.md).
Metering: [docs/usage-pools.md](docs/usage-pools.md).
