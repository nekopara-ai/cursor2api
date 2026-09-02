# Security policy

`cursor2api` handles account credentials, translates untrusted request bodies, and
can answer selected upstream file or shell operations. Security reports are
welcome and should be handled privately.

## Supported code

Security fixes are developed against the current `main` branch. If tagged releases
exist, maintainers may backport a fix when practical, but this experimental project
does not guarantee long-term support for older versions or private downstream
deployments.

## Reporting a vulnerability

Use a [private GitHub security advisory](https://github.com/nekopara-ai/cursor2api/security/advisories/new).
Do not open a public issue or pull request containing exploit details, credentials,
or a working proof of concept.

Include, where possible:

- affected commit or version;
- deployment assumptions and required configuration;
- affected route, module, or protocol path;
- impact and attacker prerequisites;
- minimal reproduction steps using synthetic credentials and data;
- whether the issue was exercised against any system; and
- a suggested mitigation or patch, if available.

The maintainers will respond on a best-effort basis. There is no guaranteed
response or disclosure SLA. Please allow a reasonable private remediation period
before publishing details.

## Appropriate research

Test only against systems and accounts you own or are explicitly authorized to
use. Keep request volume low, avoid disrupting upstream services, and stop if a
test could expose another user's data or credentials.

Do not include real Cursor tokens, API keys, cookies, account emails, user IDs,
proxy credentials, full logs, or proprietary upstream bundles in a report. Use
synthetic values and the smallest redacted evidence that still demonstrates the
issue.

## Examples of in-scope issues

- bypass of the local POST `API_KEY` check;
- unintended credential disclosure or unsafe credential-file permissions;
- path traversal outside `SANDBOX_ROOT`;
- shell execution when `SANDBOX_SHELL` is disabled;
- request parsing that enables smuggling, desynchronization, or an unintended
  body-size bypass;
- arbitrary code execution in the local process;
- unsafe handling of proxy authentication;
- an endpoint override that leaks credentials contrary to documented behavior;
- cross-request access to another caller's parked tool session;
- cross-request access to another caller's Sand Stream session;
- disclosure of the Cursor session JWT or checksum machine ids; or
- sensitive request content written to logs contrary to the documented logging
  contract.

## Generally out of scope

- vulnerabilities in Cursor or another upstream provider that are not caused by
  this repository;
- service-terms or trademark disputes without a software security defect;
- upstream model behavior, prompt injection, hallucination, or policy decisions;
- account quota, entitlement, billing, or rate-limit behavior;
- reports that only note that GET and HEAD routes are unauthenticated, because
  that is a documented current limitation;
- deployment exposure caused solely by intentionally binding the service to an
  untrusted network without an external access boundary; and
- unsupported behavior of experimental client-identity routing without a distinct
  local security impact.

If an issue affects an upstream vendor independently of this project, report it
through that vendor's security channel.

## Deployment security boundaries

The built-in server is suitable only for trusted local use unless stronger
controls are added externally:

- it provides no TLS;
- `API_KEY` protects POST routes only;
- GET routes, `/login`, and HEAD probes are not locally authenticated;
- `/health` is liveness, not upstream readiness;
- `API_KEY` is one shared secret, not user or tenant isolation;
- live tool-session state is process-local;
- Sand Stream uses the same Cursor session token plus checksum headers; and
- `sandbox.py` is a compatibility layer, not an operating-system sandbox.

Keep `BIND=127.0.0.1`, set `API_KEY`, and use a separately enforced reverse proxy,
firewall, container, or similar boundary if remote access is required. Never
enable `SANDBOX_SHELL=1` for untrusted callers.

## Credential handling

Credential candidates are considered in this order:

1. `CURSOR_ACCESS_TOKEN`;
2. `CURSOR_API_KEY`;
3. the cursor2api credential store;
4. Cursor CLI's auth file, unless disabled.

The default dedicated store is `~/.config/cursor2api/credentials.json` and is
written with mode `0600`. Set `CURSOR2API_USE_CLI_AUTH=0` when the service should
not reuse another application's credentials.

The current implementation persists a successfully exchanged `CURSOR_API_KEY`,
including one supplied through the environment, into the dedicated credential
store. Point `CURSOR2API_CREDENTIALS` at an appropriately protected location and
use `python -m cursor2api logout` when that persistence is no longer wanted.

Environment variables can be visible to process supervisors, crash reporters,
administrators, and local inspection tools. Apply normal secret-management and
least-privilege practices. Endpoint override variables can direct authentication
requests to another host; use them only in controlled tests with a trusted target.

Sand allocation returns ephemeral gateway, network, and Agent tokens. The
implementation keeps them in memory, validates that the gateway uses HTTPS on
`.cursorvm.com`, and must not include them in logs or errors.
`CURSOR_GROKBOT_URL` is a protocol test override: changing it can send the
authorization bearer to another host, so never set it to an untrusted endpoint.

## If a secret is exposed

1. revoke or rotate the affected credential through the appropriate provider;
2. stop any exposed service and remove public network reachability;
3. remove the secret from working files and logs;
4. determine whether it entered Git history, artifacts, CI logs, or backups;
5. notify affected users if other accounts or data may be involved; and
6. add a regression or guardrail that addresses the disclosure path.

Deleting a secret from the latest commit does not revoke it or remove it from
existing clones and logs.
