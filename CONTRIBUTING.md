# Contributing

## Before you start

This talks to a private Cursor protocol. Contributions that add account
marketplaces, credential stuffing, or attack tooling will be rejected.

1. Read [README.md](README.md) and [AGENTS.md](AGENTS.md).
2. Use a **personal** Cursor account you are allowed to test with.
3. Keep secrets out of the tree (see [SECURITY.md](SECURITY.md)).

## Dev setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m cursor2api login   # or export CURSOR_API_KEY
python -m cursor2api serve
```

Live tests (require the server and a working account):

```bash
python tests/test_api.py
python tests/test_openai.py
```

## Pull requests

- One concern per PR.
- Update README / README.zh-CN / `.env.example` when you add env vars or
  public routes.
- Do not reformat unrelated files.
- Describe how you tested (which endpoint, which model id, whether `sand/`
  was involved).
