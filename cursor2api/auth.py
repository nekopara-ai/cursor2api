"""Credential handling for the proxy.

Two ways to authenticate, both ending up with a short-lived Cursor access token:

  1. API key  - set CURSOR_API_KEY (a `crsr_...` key from cursor.com/dashboard).
                The key is exchanged for an access token and re-exchanged whenever
                the token is about to expire.
  2. OAuth    - `python -m cursor2api login` runs Cursor's PKCE browser login and
                stores the returned tokens locally.

Resolution order: CURSOR_ACCESS_TOKEN, CURSOR_API_KEY, the local credential file,
and finally the Cursor CLI's own auth file if it exists.

Nothing is ever written to the repository; the credential file lives under
~/.config/cursor2api/ with 0600 permissions.
"""
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

API_BASE = os.environ.get("CURSOR_API_BASE_URL", "https://api2.cursor.sh").rstrip("/")
WEBSITE = os.environ.get("CURSOR_WEBSITE_URL", "https://cursor.com").rstrip("/")

STORE = os.environ.get("CURSOR2API_CREDENTIALS") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "cursor2api", "credentials.json")
CLI_AUTH = os.path.expanduser("~/.config/cursor/auth.json")

_lock = threading.Lock()
_cached = ""


class AuthError(Exception):
    pass


# ------------------------------------------------------------------ storage
def load_store():
    try:
        with open(STORE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_store(data):
    d = os.path.dirname(STORE)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.tmp" % (STORE, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE)


def clear_store():
    try:
        os.remove(STORE)
        return True
    except OSError:
        return False


def invalidate_cached():
    """Drop the in-memory access token so the next request re-derives it.

    The cache is normally trusted until the JWT's exp; a token revoked upstream
    would otherwise keep failing with 401 until it expired on paper."""
    global _cached
    with _lock:
        _cached = ""


# ------------------------------------------------------------------ tokens
def _expiring(jwt, skew=300):
    """True when the JWT is unusable or expires within `skew` seconds."""
    try:
        body = jwt.split(".")[1]
        body += "=" * (-len(body) % 4)
        exp = json.loads(base64.urlsafe_b64decode(body)).get("exp", 0)
        return exp - time.time() < skew
    except Exception:
        return True


def _post_json(url, headers, body=b"{}", timeout=30):
    req = urllib.request.Request(url, body, dict(headers, **{
        "content-type": "application/json"}))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def exchange_api_key(key):
    """crsr_ key -> {accessToken, refreshToken}. Tokens are valid for about an hour."""
    try:
        out = _post_json(API_BASE + "/auth/exchange_user_api_key",
                         {"authorization": "Bearer " + key})
    except urllib.error.HTTPError as e:
        raise AuthError("API key rejected by Cursor (HTTP %d)" % e.code)
    if not isinstance(out, dict) or "accessToken" not in out:
        raise AuthError("unexpected response from Cursor while exchanging the API key")
    return out


def _cli_auth():
    try:
        with open(CLI_AUTH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _sources():
    """Credential candidates as (access_token, api_key, persist) triples, best first."""
    store = load_store()
    cli = _cli_auth() if os.environ.get("CURSOR2API_USE_CLI_AUTH", "1") == "1" else {}
    return [
        (os.environ.get("CURSOR_ACCESS_TOKEN", ""), "", False),
        ("", os.environ.get("CURSOR_API_KEY", ""), True),
        (store.get("accessToken", ""), store.get("apiKey", ""), True),
        (cli.get("accessToken", ""), cli.get("apiKey", ""), False),
    ]


def access_token():
    """A usable access token, refreshed if needed. Raises AuthError when there is none."""
    global _cached
    if _cached and not _expiring(_cached):
        return _cached
    with _lock:
        if _cached and not _expiring(_cached):
            return _cached
        for token, key, persist in _sources():
            if token and not _expiring(token):
                _cached = token
                return _cached
            if key:
                fresh = exchange_api_key(key)
                if persist:
                    store = load_store()
                    store.update({"apiKey": key,
                                  "accessToken": fresh["accessToken"],
                                  "refreshToken": fresh.get("refreshToken", "")})
                    save_store(store)
                _cached = fresh["accessToken"]
                return _cached
        raise AuthError(
            "no usable Cursor credentials. Set CURSOR_API_KEY, or run "
            "`python -m cursor2api login` to sign in with a browser. "
            "An OAuth session that has expired needs `login` again, because Cursor "
            "does not expose a refresh endpoint for CLI credentials.")


# ------------------------------------------------------------------ OAuth login
def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def start_login(redirect_target="cli"):
    """Build the PKCE challenge and the browser URL Cursor expects."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    handle = str(uuid.uuid4())
    url = "%s/loginDeepControl?%s" % (WEBSITE, urllib.parse.urlencode({
        "challenge": challenge, "uuid": handle, "mode": "login",
        "redirectTarget": redirect_target}))
    return {"uuid": handle, "verifier": verifier, "loginUrl": url}


def poll_login(handle, verifier, attempts=150):
    """Wait for the browser half of the flow. Returns the token pair or None."""
    url = "%s/auth/poll?%s" % (API_BASE, urllib.parse.urlencode({
        "uuid": handle, "verifier": verifier}))
    failures = 0
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                out = json.load(resp)
            if isinstance(out, dict) and "accessToken" in out:
                return out
            failures += 1
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise AuthError("Cursor rejected the login attempt (403)")
            if e.code != 404:                      # 404 = not confirmed yet
                failures += 1
        except Exception:
            failures += 1
        if failures >= 3:
            return None
        time.sleep(min(1.0 * (1.2 ** i), 10.0))
    return None


def remember(result):
    """Persist the tokens from a completed login and use them from now on."""
    global _cached
    store = load_store()
    store.update({"accessToken": result["accessToken"],
                  "refreshToken": result.get("refreshToken", "")})
    store.pop("apiKey", None)
    save_store(store)
    _cached = result["accessToken"]


def open_browser(url):
    for cmd in (["xdg-open", url], ["open", url]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            continue
    return False


def login(browser=True, out=sys.stdout):
    """Run the browser login and persist the tokens. Returns the stored account info."""
    flow = start_login()
    print("Open this URL to authorise the proxy:\n  %s\n" % flow["loginUrl"], file=out)
    if browser:
        open_browser(flow["loginUrl"])
    print("Waiting for confirmation...", file=out, flush=True)
    result = poll_login(flow["uuid"], flow["verifier"])
    if not result:
        raise AuthError("login was not completed")
    remember(result)
    return {"stored": STORE}


def whoami():
    """Account info for the current credentials, via Cursor's public API."""
    key = (os.environ.get("CURSOR_API_KEY") or load_store().get("apiKey")
           or _cli_auth().get("apiKey"))
    if not key:
        return None
    req = urllib.request.Request("https://api.cursor.com/v0/me",
                                 headers={"authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)
