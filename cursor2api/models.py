"""Model catalog for the signed-in Cursor account.

The list comes from the same RPC the CLI model picker uses:

    POST https://api2.cursor.sh/aiserver.v1.AiService/AvailableModels
    content-type: application/proto        (unary Connect, no stream framing)

AvailableModelsRequest   2 include_long_context_models  5 use_model_parameters
                         7 do_not_use_markdown
AvailableModelsResponse  2 models(AvailableModel)
AvailableModel           1 name  9 supports_thinking  10 supports_images
                         15 context_token_limit  17 client_display_name
                         29 parameter_definitions  30 variants  35 is_hidden
                         36 legacy_slugs  37 id_aliases
ParameterDefinition      1 id  2 display_name  4 values  5 default_index
ParameterValues          1 bool_options{1 value,2 label}  2 enum_options{1 value,2 label}
Variant                  1 parameters{1 id,2 value}  4/5 default flags
                         9 spec ("model[a=b,c=d]")  11 legacy_slug

A variant is a concrete parameter combination; its `legacy_slug` is the flat id
Cursor exposes elsewhere (claude-fable-5-thinking-high, composer-2.5-fast, ...).
Both spellings are accepted here, as are `model[thinking=true,effort=max]`
and a handful of Anthropic/OpenAI names.
"""
import os
import threading
import time
import urllib.error
import urllib.request

from .auth import access_token
from .pb import get, getall, getvar, msg

RPC = os.environ.get("CURSOR_AISERVER_URL", "https://api2.cursor.sh").rstrip("/") \
    + "/aiserver.v1.AiService/AvailableModels"
VERSION = os.environ.get("CURSOR_CLI_VERSION", "cli-2026.08.11-e8db854")
TTL = float(os.environ.get("MODEL_CACHE_TTL", "900"))
# A failed fetch is cached far more briefly than a good one: a network blip at
# startup used to pin /v1/models to the built-in set for the whole TTL.
FAIL_TTL = float(os.environ.get("MODEL_CACHE_FAIL_TTL", "60"))

# Used when the catalog cannot be fetched (offline, expired credentials).
FALLBACK = [
    ("claude-fable-5", {"thinking": "true", "context": "300k", "effort": "high"}),
    ("claude-opus-5", {"thinking": "true", "context": "300k", "effort": "high"}),
    ("claude-sonnet-5", {"thinking": "true", "context": "300k", "effort": "high"}),
    ("composer-2.5", {"fast": "true"}),
    ("grok-4.6", {"effort": "high", "fast": "true"}),
    ("grok-4.5", {"effort": "high", "fast": "true"}),
    ("gpt-5.6-sol", {"context": "272k", "reasoning": "medium"}),
    ("gpt-5.6-terra", {"context": "272k", "reasoning": "medium"}),
    ("kimi-k3", {"reasoning": "high"}),
]

# Names from other vendors' APIs, so existing clients keep working.
ALIASES = {
    "claude-opus-4-1": "claude-opus-5", "claude-opus-4": "claude-opus-5",
    "claude-3-opus": "claude-opus-5",
    "claude-3-7-sonnet": "claude-sonnet-5", "claude-3-5-sonnet": "claude-sonnet-5",
    "claude-3-5-haiku": "claude-haiku-4-5", "claude-3-haiku": "claude-haiku-4-5",
    "gpt-5.6": "gpt-5.6-sol", "gpt-5": "gpt-5.5", "gpt-4.1": "gpt-5.5",
    "gpt-4o-mini": "gpt-5-mini", "gpt-4o": "gpt-5.5", "o3": "gpt-5.3-codex",
    "grok": "grok-4.6", "gemini-pro": "gemini-3.1-pro", "kimi": "kimi-k3",
}

_lock = threading.Lock()
_cache = None          # (fetched_at, [model dict])


def _param_values(blob):
    """Option values of one parameter definition, in display order."""
    out = []
    for field in (1, 2):                      # bool options, then enum options
        for group in getall(blob, field):
            for opt in getall(group, 1):
                val = get(opt, 1)
                if val is not None:
                    out.append(val.decode())
    return out


def _variant(blob):
    params = {}
    for p in getall(blob, 1):
        key, val = get(p, 1), get(p, 2)
        if key is not None:
            params[key.decode()] = (val or b"").decode()
    return {
        "params": params,
        "id": (get(blob, 11) or b"").decode(),
        "spec": (get(blob, 9) or b"").decode(),
        "default": bool(getvar(blob, 4) or getvar(blob, 5)),
    }


def _model(blob):
    name = (get(blob, 1) or b"").decode()
    options, order = {}, []
    for pdef in getall(blob, 29):
        pid = (get(pdef, 1) or b"").decode()
        if not pid:
            continue
        order.append(pid)
        options[pid] = _param_values(get(pdef, 4) or b"")
    variants = [v for v in (_variant(b) for b in getall(blob, 30)) if v["params"]]
    return {
        "id": name,
        "display": (get(blob, 17) or b"").decode() or name,
        "options": options,
        "order": order,
        "variants": variants,
        "params": _defaults(name, options, variants),
        "aliases": [x.decode() for x in getall(blob, 37)],
        "slugs": [x.decode() for x in getall(blob, 36)],
        "thinking": bool(getvar(blob, 9)),
        "images": bool(getvar(blob, 10)),
        "context": getvar(blob, 15) or 0,
        "hidden": bool(getvar(blob, 35)),
    }


def _defaults(name, options, variants):
    """Parameters to send when the caller names a model without a variant."""
    for v in variants:
        if v["default"]:
            return dict(v["params"])
    for base, params in FALLBACK:
        if base == name:
            return dict(params)
    out = {}
    for pid, values in options.items():
        if not values:
            continue
        if pid == "thinking":
            out[pid] = "true" if "true" in values else values[0]
        elif pid in ("effort", "reasoning"):
            out[pid] = "high" if "high" in values else values[-1]
        else:
            out[pid] = values[0]
    return out


def fetch():
    """AvailableModels for the current credentials. Raises on transport errors."""
    body = msg(f2=True, f5=True, f7=True)
    req = urllib.request.Request(RPC, body, {
        "content-type": "application/proto",
        "connect-protocol-version": "1",
        "authorization": "Bearer " + access_token(),
        "user-agent": "connect-es/1.6.1",
        "x-cursor-client-type": "cli",
        "x-cursor-client-version": VERSION,
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    models = [_model(b) for b in getall(raw, 2)]
    return [m for m in models if m["id"] and m["id"] != "default"]


def _fallback():
    return [{"id": name, "display": name, "options": {}, "order": [],
             "variants": [], "params": dict(params), "aliases": [], "slugs": [],
             "thinking": "thinking" in params, "images": True, "context": 0,
             "hidden": False}
            for name, params in FALLBACK]


def catalog(refresh=False):
    """Cached model list. Falls back to the built-in set when the RPC fails."""
    global _cache
    now = time.time()
    if _cache and not refresh and now - _cache[0] < TTL:
        return _cache[1]
    with _lock:
        if _cache and not refresh and time.time() - _cache[0] < TTL:
            return _cache[1]
        try:
            models = fetch()
        except Exception:
            if _cache:
                return _cache[1]
            _cache = (time.time() - max(0.0, TTL - FAIL_TTL), _fallback())
            return _cache[1]
        _cache = (time.time(), models)
        return models


def options(model_id):
    """Parameter ids a model publishes, mapped to their accepted values."""
    for m in catalog():
        if m["id"] == model_id:
            return m["options"]
    return {}


def ids():
    """Every model id this proxy accepts, base models first."""
    out, seen = [], set()
    for m in catalog():
        for name in ([m["id"]] + [v["id"] for v in m["variants"]]
                     + m["slugs"] + m["aliases"]):
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _index():
    """name -> (base, params) for every spelling of every variant."""
    table = {}
    for m in catalog():
        table.setdefault(m["id"].lower(), (m["id"], m["params"]))
        for alias in m["aliases"]:
            table.setdefault(alias.lower(), (m["id"], m["params"]))
        for v in m["variants"]:
            for name in (v["id"], v["spec"]):
                if name:
                    table.setdefault(name.lower(), (m["id"], v["params"]))
        for slug in m["slugs"]:      # slugs without a variant of their own
            table.setdefault(slug.lower(), (m["id"], m["params"]))
    return table


def _from_tokens(name, table):
    """Split "<base>-<opt>-<opt>" into a base model and parameter values."""
    parts = name.split("-")
    for cut in range(len(parts) - 1, 0, -1):
        base = "-".join(parts[:cut])
        hit = table.get(base)
        if not hit:
            continue
        model = next((m for m in catalog() if m["id"] == hit[0]), None)
        if model is None:
            continue
        params = dict(hit[1])
        for tok in parts[cut:]:
            if tok in ("thinking", "think"):
                params["thinking"] = "true"
            elif tok in ("nothinking", "nonthinking"):
                params["thinking"] = "false"
            elif tok in ("fast", "max-mode"):
                params["fast"] = "true"
            else:
                for pid, values in model["options"].items():
                    if tok in values:
                        params[pid] = tok
                        break
                else:
                    return None
        return model["id"], params
    return None


def resolve(name, default="claude-fable-5"):
    """Any accepted model spelling -> (cursor model id, parameters)."""
    table = _index()
    for candidate in (name or default, default):
        got = _resolve_one((candidate or "").strip(), table)
        if got:
            return got
    return default, dict(next((m["params"] for m in catalog() if m["id"] == default), {}))


def _resolve_one(name, table):
    if not name:
        return None
    low = name.lower()
    if low in table:
        base, params = table[low]
        return base, dict(params)
    if "[" in low and low.endswith("]"):
        head, _, tail = low[:-1].partition("[")
        got = _resolve_one(head, table)
        if got:
            base, params = got
            for pair in tail.split(","):
                key, _, val = pair.partition("=")
                if key.strip():
                    params[key.strip()] = val.strip()
            return base, params
    for suffix in ("-latest", "-preview"):
        if low.endswith(suffix):
            return _resolve_one(low[: -len(suffix)], table)
    if len(low) > 9 and low[-8:].isdigit() and low[-9] == "-":
        return _resolve_one(low[:-9], table)
    tokens = _from_tokens(low, table)
    if tokens:
        return tokens
    for prefix, target in ALIASES.items():
        if low.startswith(prefix):
            hit = table.get(target)
            if hit:
                return hit[0], dict(hit[1])
    return None
