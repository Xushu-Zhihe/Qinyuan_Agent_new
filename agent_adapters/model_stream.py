"""Bounded Chat Completions SSE reader. Reasoning text is counted, never retained."""

import json


def safe_finish_reason(value):
    if value is None:
        return None
    return value if isinstance(value, str) and value in {"stop", "length", "content_filter", "tool_calls", "function_call",
                              "insufficient_system_resource"} else "other"


def numeric_usage(usage):
    numeric_usage = {}
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            value = usage.get(key)
            if type(value) is int and value >= 0:
                numeric_usage[key] = value
        for key, names in (("completion_tokens_details", ("reasoning_tokens",)),
                           ("prompt_tokens_details", ("cached_tokens",))):
            details = usage.get(key)
            if isinstance(details, dict):
                values = {name: details[name] for name in names
                          if type(details.get(name)) is int and details[name] >= 0}
                if values:
                    numeric_usage[key] = values
    return numeric_usage or None


class ModelStreamError(ValueError):
    def __init__(self, code, transport_end="incomplete_response", kind="provider_failure"):
        self.code, self.transport_end, self.kind = code, transport_end, kind
        super().__init__(code)


def read_chat_stream(response, limit, report, *, allow_tool_calls=False):
    buffer = b""
    data_lines = []
    event_bytes = 0
    content = []
    tool_call = None
    finish = None
    usage = None
    counts = {"response_utf8_bytes": 0, "content_chars": 0, "content_utf8_bytes": 0,
              "reasoning_chars": 0, "reasoning_utf8_bytes": 0, "sse_events": 0, "keepalive_lines": 0}

    def progress(end):
        report(**counts, stream_end=end, finish_reason=safe_finish_reason(finish), usage=numeric_usage(usage))

    def invalid():
        raise ModelStreamError("provider_stream_invalid")

    def event(raw):
        nonlocal finish, usage, tool_call
        counts["sse_events"] += 1
        if raw == b"[DONE]":
            if finish is None:
                invalid()
            return True
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            invalid()
        if not isinstance(value, dict):
            invalid()
        if isinstance(value.get("usage"), dict):
            usage = value["usage"]
        choices = value.get("choices")
        if choices == [] and isinstance(value.get("usage"), dict):
            return False
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            invalid()
        choice = choices[0]
        if choice.get("index", 0) != 0:
            invalid()
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            invalid()
        if delta.get("function_call"):
            invalid()
        if delta.get("tool_calls"):
            if not allow_tool_calls or finish is not None or not isinstance(delta["tool_calls"], list):
                invalid()
            for item in delta["tool_calls"]:
                if (not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] != 0
                        or set(item) - {"index", "id", "type", "function"}):
                    invalid()
                if tool_call is None:
                    tool_call = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                if "type" in item and item["type"] != "function":
                    invalid()
                if "id" in item:
                    if not isinstance(item["id"], str) or tool_call["id"] and tool_call["id"] != item["id"]:
                        invalid()
                    tool_call["id"] = item["id"]
                function = item.get("function", {})
                if not isinstance(function, dict) or set(function) - {"name", "arguments"}:
                    invalid()
                for key, part in function.items():
                    if not isinstance(part, str):
                        invalid()
                    tool_call["function"][key] += part
                    counts["content_chars"] += len(part)
                    counts["content_utf8_bytes"] += len(part.encode("utf-8"))
                if counts["content_utf8_bytes"] + counts["reasoning_utf8_bytes"] > limit:
                    raise ModelStreamError("response_rejected", "response_limit", "invalid_response")
        if delta.get("refusal"):
            raise ModelStreamError("provider_refused", "response_rejected", "refused")
        for key, prefix in (("content", "content"), ("reasoning_content", "reasoning")):
            text = delta.get(key)
            if text is None:
                continue
            if not isinstance(text, str) or (finish is not None and text):
                invalid()
            counts[prefix + "_chars"] += len(text)
            counts[prefix + "_utf8_bytes"] += len(text.encode("utf-8"))
            if counts["content_utf8_bytes"] + counts["reasoning_utf8_bytes"] > limit:
                raise ModelStreamError("response_rejected", "response_limit", "invalid_response")
            if key == "content" and text:
                content.append(text)
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str) or not reason or finish is not None:
                invalid()
            finish = reason
        return False

    try:
        while True:
            chunk = response.read1(8192)
            if not chunk:
                raise ModelStreamError("http_transport_failure")
            counts["response_utf8_bytes"] += len(chunk)
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.removesuffix(b"\r")
                if len(line) > limit:
                    raise ModelStreamError("response_rejected", "response_limit", "invalid_response")
                if not line:
                    if data_lines:
                        raw = b"\n".join(data_lines)
                        data_lines = []
                        event_bytes = 0
                        if event(raw):
                            progress("done")
                            message = {"content": "".join(content)}
                            if tool_call is not None:
                                message["tool_calls"] = [tool_call]
                            return {"choices": [{"message": message,
                                                 "finish_reason": finish}], "usage": usage}, counts["reasoning_chars"]
                elif line.startswith(b":"):
                    counts["keepalive_lines"] += 1
                elif line.startswith(b"data:"):
                    data = line[5:].removeprefix(b" ")
                    event_bytes += len(data) + bool(data_lines)
                    data_lines.append(data)
                if len(buffer) > limit and b"\n" not in buffer:
                    raise ModelStreamError("response_rejected", "response_limit", "invalid_response")
                if event_bytes > limit:
                    raise ModelStreamError("response_rejected", "response_limit", "invalid_response")
            if len(buffer) > limit:
                raise ModelStreamError("response_rejected", "response_limit", "invalid_response")
            progress("reading")
    except ModelStreamError:
        progress("incomplete")
        raise
