# Usage pools

Cursor does not meter this proxy by model name. It meters by the client identity
announced on the Run stream.

## Headers that matter

On `POST https://agentn.global.api5.cursor.sh/agent.v1.AgentService/Run`:

| header | this proxy sends | selects |
|---|---|---|
| `x-cursor-client-type` | `cli` or `sand` | **which quota bucket** |
| `x-cursor-client-version` | `CURSOR_CLI_VERSION` (a `cli-…` build) | **whether the stream is allowed** |
| `x-sand-box-namespace` | `prod` only when type is `sand` | unused for metering in current tests |
| `authorization` | Cursor access token | the account |

Isolating the headers showed:

- `sand` without a namespace header still draws the Grok Bot pool
- `cli` plus a namespace header still draws the plan pool and still 429s when
  that pool is empty
- replacing the version with Grok Bot desktop `0.18.0` returns
  `permission_denied` even with `client-type: sand`

So: keep the CLI version, change only the type.

## How the proxy exposes this

`cursor2api.session.split_client_type()`:

| model string | type | remaining model id |
|---|---|---|
| `claude-opus-5` | `CURSOR2API_CLIENT_TYPE` or `cli` | `claude-opus-5` |
| `sand/claude-opus-5` | `sand` | `claude-opus-5` |
| `bot/gpt-5.2` | `sand` | `gpt-5.2` |
| `grokbot/composer-2.5` | `sand` | `composer-2.5` |
| `cli/grok-4.6` | `cli` | `grok-4.6` |

Parked live tool sessions (`_live_sessions`) key on both model id and
`client_type`, so a `sand/` turn cannot be resumed by a `cli/` follow-up.

## Dashboard RPCs (read-only)

Same access token, Connect JSON, empty body, `api2.cursor.sh`:

- `aiserver.v1.DashboardService/GetSandAccessStatus`
  - `SAND_ACCESS_STATE_GRANTED` means the account may use Grok Bot
  - `proAndSuperGrokPlansGrantAccess` reflects the 2026-08-26 plan expansion
- `aiserver.v1.DashboardService/GetSandUsageStatus`
  - `usagePercent`, `nextResetTimestampUtc`, `grokPlanLabel`
  - independent of `GET https://cursor.com/api/usage-summary` (plan pool)

## What this is not

- Not xAI `api.x.ai` (that wallet is a Console API key, not the subscription).
- Not Grok Web / Grok Build SSO gateways (`cli-chat-proxy.grok.com`).
- Not Cursor Cloud Agents (those consume included usage, then on-demand).
- Not a substitute for the Grok Bot desktop/iOS app (no cloud computer, no
  connectors). Only the **inference meter** is shared.

## Risk

`sand` is the production client-type of the official Grok Bot desktop app
(`com.anysphere.sand`). Using it from this CLI transport is unofficial.
Upstream can bind the type to a desktop version, a machine checksum, or both.
Treat the prefix as best-effort and keep request volume ordinary.
