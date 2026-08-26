# Security

## Reporting

If you find a vulnerability in **this proxy** (auth bypass, credential leak,
path traversal in the sandbox, etc.), open a private GitHub security advisory
on this repository. Do not file a public issue with working exploit code.

This project does not accept reports whose only content is "this violates
Cursor's terms of service". That is a policy question for Cursor, not a
security bug in the proxy.

## Credential handling

- Default store: `~/.config/cursor2api/credentials.json` (mode 0600).
- Override with `CURSOR2API_CREDENTIALS`.
- Resolution order: `CURSOR_ACCESS_TOKEN`, `CURSOR_API_KEY`, the store, then
  the Cursor CLI file `~/.config/cursor/auth.json`.
- The repository must never contain real tokens. `.gitignore` already lists
  `.env`, `credentials.json`, `*.pem`, logs.

## Deployment

- Bind to `127.0.0.1` unless you fully understand the exposure.
- Set `API_KEY` so local clients must authenticate to the proxy.
- Treat `sand/` as unofficial client impersonation: it can break without
  notice and may be treated as abuse by the upstream.
- Watch on-demand / hard-limit settings on the Cursor dashboard so a drained
  Grok Bot pool does not silently start billing.

## Supply chain

The only runtime dependency is `h2` (and its `hpack` / `hyperframe`
transitive deps). Review `requirements.txt` before bumping.
