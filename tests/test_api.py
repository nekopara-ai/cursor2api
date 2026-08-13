import base64, json, os, urllib.request, zlib

B = os.environ.get("BASE", "http://127.0.0.1:8787")


def post(path, body, stream=False):
    r = urllib.request.Request(B + path, json.dumps(body).encode(),
                               {"content-type": "application/json"})
    resp = urllib.request.urlopen(r, timeout=280)
    if stream:
        return resp.read().decode()
    return json.load(resp)


def png():
    def chunk(t, d):
        c = t + d
        return len(d).to_bytes(4, "big") + c + zlib.crc32(c).to_bytes(4, "big")
    n = 64
    raw = b"\x00" + b"\x00\x00\xff" * n
    idat = zlib.compress(raw * n)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", n.to_bytes(4, "big") + n.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0]))
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def pdf(text):
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    body = f"BT /F1 18 Tf 30 120 Td ({text}) Tj ET"
    objs[3] = f"<< /Length {len(body)} >>\nstream\n{body}\nendstream"
    out = b"%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return out


def show(name, d):
    kinds = [(b["type"], (b.get("text") or b.get("thinking") or json.dumps(b.get("input", ""),
             ensure_ascii=False))[:70]) for b in d["content"]]
    print(f"[{name}] stop={d['stop_reason']} usage={d['usage']}")
    for k, v in kinds:
        print("   ", k, "|", v.replace("\n", " "))


tests = []

tests.append(("text", lambda: show("text", post("/v1/messages", {
    "model": "claude-sonnet-4-5", "max_tokens": 200,
    "messages": [{"role": "user", "content": "用一句话说明什么是黑洞"}]}))))

tests.append(("system+multiturn", lambda: show("system+multiturn", post("/v1/messages", {
    "model": "claude-sonnet-4-5", "max_tokens": 200,
    "system": [{"type": "text", "text": "你必须只用一个词回答，且总是大写。"}],
    "messages": [{"role": "user", "content": "法国的首都?"},
                 {"role": "assistant", "content": "PARIS"},
                 {"role": "user", "content": "日本的首都?"}]}))))

tests.append(("thinking", lambda: show("thinking", post("/v1/messages", {
    "model": "claude-opus-4-5", "max_tokens": 600,
    "thinking": {"type": "enabled", "budget_tokens": 3000},
    "messages": [{"role": "user", "content": "仔细推理：有5顶帽子3红2蓝，三人一列各戴一顶只看得到前面的人，"
                                             "最后一人说不知道，中间人说不知道，第一人说知道了，为什么？"}]}))))

tests.append(("image", lambda: show("image", post("/v1/messages", {
    "model": "claude-sonnet-4-5", "max_tokens": 100,
    "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(png()).decode()}},
        {"type": "text", "text": "这张图是什么颜色？只回答颜色。"}]}]}))))

tests.append(("pdf", lambda: show("pdf", post("/v1/messages", {
    "model": "claude-sonnet-4-5", "max_tokens": 200,
    "messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                        "data": base64.b64encode(pdf("SECRET CODE: ZEBRA42")).decode()}},
        {"type": "text", "text": "这个 PDF 里的 secret code 是什么？只回答 code。"}]}]}))))

TOOL = [{"name": "get_weather", "description": "Get the current weather in a location",
         "input_schema": {"type": "object", "properties": {
             "location": {"type": "string"}, "unit": {"type": "string", "enum": ["c", "f"]}},
             "required": ["location"]}}]


def tool_roundtrip():
    d = post("/v1/messages", {"model": "claude-sonnet-4-5", "max_tokens": 300,
                              "tools": TOOL,
                              "messages": [{"role": "user", "content": "上海今天天气怎么样？"}]})
    show("tool_use", d)
    tu = [b for b in d["content"] if b["type"] == "tool_use"]
    if not tu:
        return
    d2 = post("/v1/messages", {"model": "claude-sonnet-4-5", "max_tokens": 300, "tools": TOOL,
                               "messages": [
                                   {"role": "user", "content": "上海今天天气怎么样？"},
                                   {"role": "assistant", "content": d["content"]},
                                   {"role": "user", "content": [
                                       {"type": "tool_result", "tool_use_id": tu[0]["id"],
                                        "content": "31C, sunny"}]}]})
    show("tool_result", d2)


tests.append(("tools", tool_roundtrip))

tests.append(("websearch", lambda: show("websearch", post("/v1/messages", {
    "model": "claude-sonnet-4-5", "max_tokens": 400,
    "messages": [{"role": "user", "content": "联网查一下 Python 3.13 的正式发布日期，一句话回答并给链接。"}]}))))


def streaming():
    raw = post("/v1/messages", {"model": "claude-sonnet-4-5", "max_tokens": 200, "stream": True,
                                "messages": [{"role": "user", "content": "数到5，只输出数字"}]},
               stream=True)
    evs = [l[7:] for l in raw.splitlines() if l.startswith("event: ")]
    txt = "".join(json.loads(l[6:])["delta"].get("text", "")
                  for l in raw.splitlines()
                  if l.startswith("data: ") and '"text_delta"' in l)
    print("[stream] events=", evs)
    print("    text |", txt.replace("\n", " ")[:80])


tests.append(("stream", streaming))

tests.append(("stop_sequences", lambda: show("stop_sequences", post("/v1/messages", {
    "model": "claude-sonnet-4-5", "max_tokens": 500, "stop_sequences": ["3"],
    "messages": [{"role": "user", "content": "只输出：1 2 3 4 5"}]}))))

tests.append(("max_tokens", lambda: show("max_tokens", post("/v1/messages", {
    "model": "claude-sonnet-4-5", "max_tokens": 5,
    "messages": [{"role": "user", "content": "用200字介绍长江"}]}))))

tests.append(("tool_choice", lambda: show("tool_choice", post("/v1/messages", {
    "model": "claude-sonnet-4-5", "max_tokens": 300, "tools": TOOL,
    "tool_choice": {"type": "tool", "name": "get_weather"},
    "messages": [{"role": "user", "content": "上海怎么样"}]}))))

tests.append(("ignored_params", lambda: show("ignored_params", post("/v1/messages", {
    "model": "claude-sonnet-4-5", "max_tokens": 100, "temperature": 0.2, "top_p": 0.9, "top_k": 5,
    "metadata": {"user_id": "u1"},
    "system": [{"type": "text", "text": "简洁。", "cache_control": {"type": "ephemeral"}}],
    "messages": [{"role": "user", "content": "1+1=?"}]}))))


def misc():
    print("[models]", [m["id"] for m in json.load(urllib.request.urlopen(B + "/v1/models"))["data"]])
    print("[count_tokens]", post("/v1/messages/count_tokens",
                                 {"model": "claude-sonnet-4-5",
                                  "messages": [{"role": "user", "content": "hello"}]}))
    try:
        post("/v1/messages", {"model": "claude-sonnet-4-5", "max_tokens": 10, "messages": []})
    except urllib.error.HTTPError as e:
        print("[empty messages]", e.code, e.read().decode()[:90])


tests.append(("misc", misc))

only = None
import sys
if len(sys.argv) > 1:
    only = sys.argv[1:]
for name, fn in tests:
    if only and name not in only:
        continue
    try:
        fn()
    except Exception as e:
        print(f"[{name}] FAILED {type(e).__name__}: {e}")
