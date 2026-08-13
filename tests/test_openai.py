"""OpenAI chat-completions checks against a running proxy (python -m cursor2api serve)."""
import base64, json, os, sys, urllib.error, urllib.request, zlib

B = os.environ.get("BASE", "http://127.0.0.1:8787")
M = os.environ.get("MODEL", "claude-fable-5")
CHAT = "/v1/chat/completions"


def post(body, raw=False):
    r = urllib.request.Request(B + CHAT, json.dumps(body).encode(),
                               {"content-type": "application/json"})
    resp = urllib.request.urlopen(r, timeout=280)
    return resp.read().decode() if raw else json.load(resp)


def png(n=64):
    def chunk(t, d):
        c = t + d
        return len(d).to_bytes(4, "big") + c + zlib.crc32(c).to_bytes(4, "big")
    head = n.to_bytes(4, "big") * 2 + bytes([8, 2, 0, 0, 0])
    body = zlib.compress(b"".join(b"\x00" + b"\x00\x00\xff" * n for _ in range(n)))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head)
            + chunk(b"IDAT", body) + chunk(b"IEND", b""))


def show(name, d):
    m = d["choices"][0]["message"]
    print(f"[{name}] model={d['model']} finish={d['choices'][0]['finish_reason']} "
          f"usage={d['usage']}")
    for key in ("reasoning_content", "content"):
        if m.get(key):
            print("   ", key, "|", m[key][:70].replace("\n", " "))
    for c in m.get("tool_calls") or []:
        print("    tool_call |", c["function"]["name"], c["function"]["arguments"])


def deltas(raw):
    events = []
    for line in raw.splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            events.append(json.loads(line[6:]))
    return events


TOOL = [{"type": "function", "function": {
    "name": "get_weather", "description": "Get the current weather in a location",
    "parameters": {"type": "object", "properties": {
        "location": {"type": "string"}, "unit": {"type": "string", "enum": ["c", "f"]}},
        "required": ["location"]}}}]

tests = []

tests.append(("text", lambda: show("text", post({
    "model": "gpt-5.6-sol", "max_tokens": 200,
    "messages": [{"role": "system", "content": "Answer with one word, uppercase."},
                 {"role": "user", "content": "Capital of Japan?"}]}))))

tests.append(("multiturn", lambda: show("multiturn", post({
    "model": M,
    "messages": [{"role": "user", "content": "法国的首都?"},
                 {"role": "assistant", "content": "巴黎"},
                 {"role": "user", "content": "那日本的呢？只答城市名。"}]}))))

tests.append(("reasoning", lambda: show("reasoning", post({
    "model": M + "-thinking-high", "reasoning_effort": "high",
    "messages": [{"role": "user", "content": "仔细推理：3 个人 3 天用 3 桶水，"
                                             "9 个人 9 天用几桶水？"}]}))))

tests.append(("image", lambda: show("image", post({
    "model": M,
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "这张图是什么颜色？只回答颜色。"},
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(png()).decode()}}]}]}))))


def tools():
    d = post({"model": M, "tools": TOOL,
              "messages": [{"role": "user", "content": "上海现在天气怎么样？用工具查。"}]})
    show("tools", d)
    call = d["choices"][0]["message"]["tool_calls"][0]
    d2 = post({"model": M, "tools": TOOL, "messages": [
        {"role": "user", "content": "上海现在天气怎么样？用工具查。"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"],
         "content": json.dumps({"temp_c": 31, "condition": "sunny"})}]})
    show("tool_result", d2)


tests.append(("tools", tools))

tests.append(("tool_choice", lambda: show("tool_choice", post({
    "model": M, "tools": TOOL,
    "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
    "messages": [{"role": "user", "content": "上海天气"}]}))))


def stream():
    raw = post({"model": M, "stream": True,
                "messages": [{"role": "user", "content": "数到 5，用空格分隔。"}]}, raw=True)
    ev = deltas(raw)
    text = "".join(e["choices"][0]["delta"].get("content") or "" for e in ev)
    print("[stream] chunks=%d done=%s finish=%s usage=%s" % (
        len(ev), "[DONE]" in raw, ev[-1]["choices"][0]["finish_reason"],
        ev[-1].get("usage")))
    print("    text |", text.strip()[:70])


tests.append(("stream", stream))


def stream_tools():
    raw = post({"model": M, "stream": True, "tools": TOOL,
                "messages": [{"role": "user", "content": "北京天气？用工具查。"}]}, raw=True)
    ev = deltas(raw)
    calls = [c for e in ev for c in e["choices"][0]["delta"].get("tool_calls") or []]
    print("[stream_tools] finish=%s calls=%s" % (
        ev[-1]["choices"][0]["finish_reason"],
        [(c["function"]["name"], c["function"]["arguments"]) for c in calls]))


tests.append(("stream_tools", stream_tools))

tests.append(("stop", lambda: show("stop", post({
    "model": M, "stop": ["3"], "max_completion_tokens": 50,
    "messages": [{"role": "user", "content": "数到 9，用空格分隔。"}]}))))

tests.append(("ignored_params", lambda: show("ignored_params", post({
    "model": M, "temperature": 0.1, "top_p": 0.5, "n": 1,
    "seed": 7, "presence_penalty": 0.2, "response_format": {"type": "text"},
    "messages": [{"role": "user", "content": "1+1=? 只回答数字。"}]}))))


def variants():
    for name in ("fable", "claude-sonnet-5-thinking-low", "composer-2.5",
                 "gpt-4o", "claude-3-5-sonnet-20241022", "claude-fable-5[effort=low]"):
        d = post({"model": name, "max_tokens": 20,
                  "messages": [{"role": "user", "content": "say OK"}]})
        print("[variant] %-34s -> %s" % (name, (d["choices"][0]["message"]["content"] or "")[:20]))


tests.append(("variants", variants))


def misc():
    data = json.load(urllib.request.urlopen(B + "/v1/models"))["data"]
    print("[models] count=%d first=%s" % (len(data), [m["id"] for m in data[:3]]))
    one = json.load(urllib.request.urlopen(B + "/v1/models/" + data[0]["id"]))
    print("[model] ", {k: one[k] for k in ("id", "object", "owned_by")})
    try:
        post({"model": M, "messages": []})
    except urllib.error.HTTPError as e:
        print("[empty messages]", e.code, e.read().decode()[:80])


tests.append(("misc", misc))

only = sys.argv[1:] or None
for name, fn in tests:
    if only and name not in only:
        continue
    try:
        fn()
    except Exception as e:
        print(f"[{name}] FAILED {type(e).__name__}: {e}")
