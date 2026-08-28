# Contributing to cursor2api

Thank you for helping improve `cursor2api`. Contributions should make the local
gateway more correct, understandable, testable, or secure while respecting that
it interacts with a private upstream protocol and real user accounts.

## Before opening a change

- Read [README.md](README.md), [Architecture](docs/architecture.md), and
  [AGENTS.md](AGENTS.md).
- Search existing issues and pull requests before starting substantial work.
- Open an issue first when a proposal adds a dependency, changes a public API
  shape, alters credential handling, or substantially changes the upstream
  protocol implementation.
- Report security vulnerabilities privately according to
  [SECURITY.md](SECURITY.md), not through a public issue.

## Project scope

In-scope contributions include:

- compatibility fixes for the documented Anthropic and OpenAI API subsets;
- transport, framing, timeout, and tool-continuation correctness;
- authentication and local deployment hardening;
- model-catalog and parameter-resolution improvements;
- offline regression coverage;
- documentation and diagnostics; and
- narrowly scoped protocol research required to maintain compatibility.

The project will not accept features for credential theft, credential stuffing,
account farming, token or account marketplaces, quota evasion, malware, public
exploitation, or unauthorized access. Do not submit proprietary application
bundles, complete packet captures, account data, or third-party source code that
the repository is not licensed to redistribute.

## Development setup

```bash
git clone https://github.com/nekopara-ai/cursor2api.git
cd cursor2api
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Runtime support starts at Python 3.9. Keep the dependency surface small; the
current direct runtime dependency is `h2`.

Do not place real credentials in the repository. For optional live tests, export a
credential in your shell or use the normal browser-login store outside the tree.
The application does not automatically load `.env` files.

## Test categories

### Offline regression suite

Run this for every code change:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python tests/test_timeouts_offline.py
```

The first command covers transport resilience, protobuf tool values, exec routing,
and related offline behavior. The timeout script is retained as a standalone
regression and should also pass.

### Live integration scripts

`tests/test_api.py` and `tests/test_openai.py` call a separately running local
server and, through it, a real Cursor account:

```bash
BIND=127.0.0.1 API_KEY= python -m cursor2api serve

# In another shell:
BASE=http://127.0.0.1:8787 python tests/test_api.py
BASE=http://127.0.0.1:8787 MODEL=claude-fable-5 python tests/test_openai.py
```

The scripts do not currently add a local `API_KEY`, so use an unprotected
localhost-only test listener or adapt the scripts locally for the gate. Never use
this setup on a shared or remotely reachable interface. Run the scripts only with
a personal test account you are authorized to use.
They consume account capacity and can be affected by model availability, account
policy, upstream changes, and rate limits. State clearly which live cases were and
were not run in a pull request.

Do not use experimental client-identity routing merely to make a test pass. If it
is part of the behavior under test, identify the prefix and compare it with the
default `cli` path.

## Making a change

### HTTP or configuration behavior

When adding or changing a route, request field, response field, environment
variable, default, authentication rule, or health semantic, update the relevant
files in the same pull request:

- [API reference](docs/api-reference.md);
- [Configuration](docs/configuration.md);
- [`.env.example`](.env.example);
- [Operations](docs/operations.md); and
- both README files when the public project overview or quick start changes.

### Protocol behavior

Protocol changes should include:

- the exact affected message, field, header, or framing behavior;
- evidence that distinguishes observation from inference;
- the Cursor client-version context in which it was observed;
- an offline regression whenever the behavior can be reproduced without an
  account; and
- an update to [docs/protocol.md](docs/protocol.md) when the documented contract
  changes.

Avoid large opaque dumps. Reduce captures to the smallest de-identified fixture
that preserves the bug and that the project is permitted to redistribute.

### Security-sensitive behavior

Changes involving credentials, listener exposure, request parsing, sandbox paths,
shell execution, proxy authentication, or endpoint overrides require explicit
threat-boundary documentation and negative tests where practical.

The compatibility sandbox is not an OS isolation boundary. Do not describe path
rewriting as containment, and do not enable shell execution by default.

## Code style

- Support Python 3.9 and newer.
- Follow the existing compact module style without reformatting unrelated code.
- Use English for code comments and user-facing reference documentation.
- Prefer standard-library code plus the existing dependency unless a new
  dependency has a clear maintenance and security justification.
- Keep changes focused; separate refactors from behavior fixes when practical.
- Do not silently swallow a new failure mode unless the protocol requires a
  best-effort fallback and that fallback is documented.

No repository-wide formatter is currently enforced. At minimum, inspect the diff
and run:

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 python -c \
  'import cursor2api.auth, cursor2api.h2stream, cursor2api.models, cursor2api.server, cursor2api.session'
```

## Pull-request expectations

A pull request should explain:

- the user-visible problem or protocol failure;
- the root cause and why the chosen fix is scoped correctly;
- security and compatibility implications;
- offline tests run and their results;
- live tests run, including model and client identity, or why they were not run;
- documentation updated; and
- remaining unverified behavior.

Before submission, verify that the diff contains no credentials, tokens, account
identifiers, proxy passwords, captured traffic, generated caches, logs, or local
runtime files.

Maintainers may ask for a smaller patch, a reproducible offline test, or additional
evidence before accepting changes to the private-protocol layer.
