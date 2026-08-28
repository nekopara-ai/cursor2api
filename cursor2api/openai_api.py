"""OpenAI Chat Completions <-> Anthropic Messages translation.

`POST /v1/chat/completions` requests are rewritten into the Anthropic request
shape the rest of the proxy already speaks, and the resulting blocks/deltas are
rendered back as `chat.completion` / `chat.completion.chunk` objects.

Mapping notes
  system / developer role      -> Anthropic `system`
  content parts                -> text, image_url (data: URLs only), file
  assistant tool_calls         -> tool_use blocks
  role "tool"                  -> tool_result blocks
  tools[].function             -> Anthropic tools
  tool_choice auto/none/required/{function} -> Anthropic tool_choice
  reasoning_effort / thinking   -> Anthropic thinking (reasoning_content on the way back)
  temperature, top_p, n, seed, penalties: accepted and ignored (no protocol knob)
"""
import base64
import json
import time
import uuid


def _data_url(url):
    """data:<mime>;base64,<payload> -> (bytes, mime). Returns (None, "") otherwise."""
    if not url.startswith("data:") or ";base64," not in url:
        return None, ""
    head, _, payload = url.partition(";base64,")
    try:
        return base64.b64decode(payload), head[5:]
    except Exception:
        return None, ""


def _parts(content):
    """OpenAI content (str or parts) -> Anthropic content blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out = []
    for p in content:
        if not isinstance(p, dict):
            continue
        kind = p.get("type")
        if kind in ("text", "input_text"):
            out.append({"type": "text", "text": p.get("text", "")})
        elif kind in ("image_url", "input_image"):
            url = p.get("image_url", {}).get("url") if kind == "image_url" else p.get("image_url", "")
            data, mime = _data_url(url or "")
            if data:
                out.append({"type": "image", "source": {
                    "type": "base64", "media_type": mime or "image/png",
                    "data": base64.b64encode(data).decode()}})
            elif url:
                out.append({"type": "text", "text": "[image %s]" % url})
        elif kind in ("file", "input_file"):
            f = p.get("file") or p
            data, mime = _data_url(f.get("file_data", "") or "")
            if data:
                out.append({"type": "document", "title": f.get("filename", "document.pdf"),
                            "source": {"type": "base64",
                                       "media_type": mime or "application/pdf",
                                       "data": base64.b64encode(data).decode()}})
    return out


def to_anthropic(body):
    """OpenAI chat request -> Anthropic messages request."""
    system, messages = [], []
    for m in body.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            text = m.get("content")
            system.append(text if isinstance(text, str) else
                          "\n".join(b["text"] for b in _parts(text) if b["type"] == "text"))
            continue
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                     "content": m.get("content") if isinstance(m.get("content"), str)
                     else json.dumps(m.get("content"), ensure_ascii=False)}
            if messages and messages[-1]["role"] == "user":
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})
            continue
        blocks = _parts(m.get("content"))
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_raw": fn.get("arguments", "")}
            blocks.append({"type": "tool_use", "id": call.get("id", ""),
                           "name": fn.get("name", ""), "input": args})
        if blocks:
            messages.append({"role": "assistant" if role == "assistant" else "user",
                             "content": blocks})

    out = {"model": body.get("model", ""), "messages": messages,
           "stream": bool(body.get("stream"))}
    if system:
        out["system"] = "\n\n".join(x for x in system if x)
    # max_completion_tokens is the newer spelling and wins when both are sent;
    # the other order silently capped a 32k request at a legacy max_tokens.
    for src in ("max_tokens", "max_completion_tokens"):
        if body.get(src):
            out["max_tokens"] = body[src]
    stop = body.get("stop")
    if stop:
        out["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    effort = body.get("reasoning_effort")
    if effort in (None, "none") and isinstance(body.get("reasoning"), dict):
        effort = body["reasoning"].get("effort")
    thinking = body.get("thinking")
    if effort not in (None, "none") or thinking:
        # Carry the level through, not just the on/off bit: "low" and "high"
        # used to reach the backend as the same request.
        out["thinking"] = {"type": "enabled"}
        if effort:
            out["thinking"]["effort"] = effort
        if isinstance(thinking, dict) and thinking.get("budget_tokens"):
            out["thinking"]["budget_tokens"] = thinking["budget_tokens"]

    tools = []
    for t in body.get("tools") or []:
        fn = t.get("function") if t.get("type") in (None, "function") else None
        if not fn:
            continue
        tools.append({"name": fn.get("name", ""), "description": fn.get("description", ""),
                      "input_schema": fn.get("parameters") or {"type": "object"}})
    if tools:
        out["tools"] = tools
    choice = body.get("tool_choice")
    if choice == "none":
        out["tool_choice"] = {"type": "none"}
    elif choice == "required":
        out["tool_choice"] = {"type": "any"}
    elif isinstance(choice, dict):
        out["tool_choice"] = {"type": "tool",
                              "name": (choice.get("function") or {}).get("name", "")}
    return out


FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
          "tool_use": "tool_calls"}


def usage(u):
    return {"prompt_tokens": u.get("input_tokens", 0),
            "completion_tokens": u.get("output_tokens", 0),
            "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0),
            "prompt_tokens_details": {
                "cached_tokens": u.get("cache_read_input_tokens", 0)}}


def completion(model, text, thinking, tool_calls, stop_reason, u):
    """Non-streaming chat.completion object."""
    message = {"role": "assistant", "content": text or None}
    if thinking:
        message["reasoning_content"] = thinking
    if tool_calls:
        message["tool_calls"] = [
            {"id": t["id"], "type": "function",
             "function": {"name": t["name"],
                          "arguments": json.dumps(t["input"], ensure_ascii=False)}}
            for t in tool_calls]
    return {"id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": message, "logprobs": None,
                         "finish_reason": FINISH.get(stop_reason, "stop")}],
            "usage": usage(u)}


def chunk(cid, model, delta, finish=None, u=None):
    """One chat.completion.chunk."""
    out = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
           "model": model,
           "choices": [{"index": 0, "delta": delta, "logprobs": None,
                        "finish_reason": finish}]}
    if u is not None:
        out["usage"] = usage(u)
    return out


def tool_call_delta(index, call):
    return {"tool_calls": [{"index": index, "id": call["id"], "type": "function",
                            "function": {"name": call["name"],
                                         "arguments": json.dumps(call["input"],
                                                                 ensure_ascii=False)}}]}
