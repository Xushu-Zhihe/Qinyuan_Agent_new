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
    tool_calls = {}
    tool_metadata_bytes = 0
    finish = None
    done_seen = False
    stream_error = None
    usage = None
    counts = {"response_utf8_bytes": 0, "content_chars": 0, "content_utf8_bytes": 0,
              "reasoning_chars": 0, "reasoning_utf8_bytes": 0, "sse_events": 0, "keepalive_lines": 0}

    def progress(end):
        report(**counts, stream_end=end, finish_reason=safe_finish_reason(finish), usage=numeric_usage(usage),
               finish_seen=finish is not None, done_seen=done_seen, stream_error=stream_error)

    def invalid(branch, path, value):
        nonlocal stream_error
        stream_error = {"branch": branch, "event_index": counts["sse_events"], "field_path": path,
                        "observed_type": "null" if value is None else type(value).__name__}
        raise ModelStreamError("provider_stream_invalid")

    def event(raw):
        nonlocal finish, usage, tool_metadata_bytes, done_seen
        counts["sse_events"] += 1
        if raw == b"[DONE]":
            done_seen = True
            if finish is None:
                invalid("done_without_finish", "$.choices[0].finish_reason", finish)
            if sorted(tool_calls) != list(range(len(tool_calls))):
                invalid("tool_index_sequence_invalid", "$.choices[0].delta.tool_calls", list(tool_calls))
            return True
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            invalid("event_json_invalid", "$", raw)
        if not isinstance(value, dict):
            invalid("event_type_invalid", "$", value)
        if isinstance(value.get("usage"), dict):
            usage = value["usage"]
        choices = value.get("choices")
        if choices == [] and isinstance(value.get("usage"), dict):
            return False
        if not isinstance(choices, list):
            invalid("choices_type_invalid", "$.choices", choices)
        if len(choices) != 1:
            invalid("choices_count_invalid", "$.choices", choices)
        if not isinstance(choices[0], dict):
            invalid("choice_type_invalid", "$.choices[0]", choices[0])
        choice = choices[0]
        if choice.get("index", 0) != 0:
            invalid("choice_index_invalid", "$.choices[0].index", choice.get("index"))
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            invalid("delta_type_invalid", "$.choices[0].delta", delta)
        if delta.get("function_call"):
            invalid("legacy_function_call", "$.choices[0].delta.function_call", delta["function_call"])
        if delta.get("tool_calls"):
            if not allow_tool_calls or finish is not None or not isinstance(delta["tool_calls"], list):
                invalid("tool_calls_not_allowed" if not allow_tool_calls else "tool_calls_after_finish" if finish is not None
                        else "tool_calls_type_invalid", "$.choices[0].delta.tool_calls", delta["tool_calls"])
            for position, item in enumerate(delta["tool_calls"]):
                path = f"$.choices[0].delta.tool_calls[{position}]"
                if not isinstance(item, dict):
                    invalid("tool_item_type_invalid", path, item)
                if type(item.get("index")) is not int or item["index"] < 0:
                    invalid("tool_index_invalid", path + ".index", item.get("index"))
                if set(item) - {"index", "id", "type", "function"}:
                    invalid("tool_fields_invalid", path, item)
                index = item["index"]
                if index not in tool_calls:
                    tool_call = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    tool_calls[index] = tool_call
                    tool_metadata_bytes += len(json.dumps(tool_call).encode("utf-8"))
                tool_call = tool_calls[index]
                if "type" in item and item["type"] != "function":
                    invalid("tool_type_invalid", path + ".type", item["type"])
                if "id" in item:
                    if not isinstance(item["id"], str):
                        invalid("tool_id_type_invalid", path + ".id", item["id"])
                    if tool_call["id"] and tool_call["id"] != item["id"]:
                        invalid("tool_id_changed", path + ".id", item["id"])
                    if not tool_call["id"]:
                        tool_metadata_bytes += len(json.dumps(item["id"], ensure_ascii=False).encode("utf-8")) - 2
                    tool_call["id"] = item["id"]
                function = item.get("function", {})
                if not isinstance(function, dict):
                    invalid("tool_function_type_invalid", path + ".function", function)
                if set(function) - {"name", "arguments"}:
                    invalid("tool_function_fields_invalid", path + ".function", function)
                for key, part in function.items():
                    if not isinstance(part, str):
                        invalid("tool_function_field_type", path + ".function." + key, part)
                    tool_call["function"][key] += part
                    counts["content_chars"] += len(part)
                    counts["content_utf8_bytes"] += len(part.encode("utf-8"))
                if counts["content_utf8_bytes"] + counts["reasoning_utf8_bytes"] + tool_metadata_bytes > limit:
                    raise ModelStreamError("response_rejected", "response_limit", "invalid_response")
        if delta.get("refusal"):
            raise ModelStreamError("provider_refused", "response_rejected", "refused")
        for key, prefix in (("content", "content"), ("reasoning_content", "reasoning")):
            text = delta.get(key)
            if text is None:
                continue
            if not isinstance(text, str) or (finish is not None and text):
                invalid("text_after_finish" if finish is not None and isinstance(text, str) else "text_type_invalid",
                        "$.choices[0].delta." + key, text)
            counts[prefix + "_chars"] += len(text)
            counts[prefix + "_utf8_bytes"] += len(text.encode("utf-8"))
            if counts["content_utf8_bytes"] + counts["reasoning_utf8_bytes"] + tool_metadata_bytes > limit:
                raise ModelStreamError("response_rejected", "response_limit", "invalid_response")
            if key == "content" and text:
                content.append(text)
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str) or not reason or finish is not None:
                invalid("finish_reason_invalid", "$.choices[0].finish_reason", reason)
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
                            if tool_calls:
                                message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
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
