# Experimental client identity routing

`cursor2api` can change the upstream `x-cursor-client-type` header globally or for
one request. This is an experimental interoperability feature, not a stable API or
a promise about account quotas, billing, eligibility, or continued upstream
acceptance.

> [!WARNING]
> A client identity can affect how Cursor handles and meters a request. Use only
> with an account you control, review the account's official usage and billing
> settings, and assume that upstream validation can change without notice.

## Configuration

The process-wide default is:

```bash
export CURSOR2API_CLIENT_TYPE=cli
```

The model string can override it for one request:

| Model string | Announced client type | Resolved model string |
|---|---|---|
| `claude-fable-5` | `CURSOR2API_CLIENT_TYPE` (default `cli`) | `claude-fable-5` |
| `cli/claude-fable-5` | `cli` | `claude-fable-5` |
| `sand/claude-fable-5` | `sand` | `claude-fable-5` |
| `bot/claude-fable-5` | `sand` | `claude-fable-5` |
| `grokbot/claude-fable-5` | `sand` | `claude-fable-5` |

The prefix is removed before normal model resolution. It does not select an xAI
API endpoint and does not turn a model into a different provider's API product.

## Headers sent upstream

For the Run stream, the relevant relationship is:

| Header | Value |
|---|---|
| `authorization` | Access token for the Cursor account |
| `x-cursor-client-type` | `cli` or `sand` |
| `x-cursor-client-version` | `CURSOR_CLI_VERSION`, which remains a compatible Cursor CLI build ID |
| `x-sand-box-namespace` | `prod` when the selected type is `sand` |

Changing the client type does not authorize an arbitrary client-version value.
The proxy continues to speak the Cursor CLI form of `AgentService/Run`, so the
version header must remain compatible with that transport.

## Live tool-session rule

A parked tool stream retains the client identity with which it was opened. A
tool-result follow-up can resume it only when the resolved model and client type
both match and the complete tool-result set is returned. Changing from `cli/` to
`sand/`, or the reverse, causes the fresh replay path.

## What this feature does not guarantee

- It does not guarantee that the upstream accepts `sand` from this transport.
- It does not guarantee that a model is available to the account.
- It does not report or guarantee a quota bucket, reset schedule, included usage,
  or on-demand billing behavior.
- It does not implement the xAI API or use an xAI Console API key.
- It does not reproduce the features of any desktop, mobile, cloud-agent, or
  subscription product associated with a client identity.
- It does not provide a way to bypass account authorization or provider policy.

Use official Cursor account surfaces as the authority for eligibility, usage, and
billing. A request succeeding today is not evidence that this behavior is stable
or supported.

## Compatibility risk

The upstream can bind a client type to additional properties such as a particular
version, application build, device state, machine identifier, or attestation.
It can also remove or rename the type. Any of those changes can cause prefixed
requests to fail independently of the base model and credential.

For diagnosis, first remove the prefix and compare the same request using the
default `cli` identity. Keep this experimental feature out of critical workflows.
