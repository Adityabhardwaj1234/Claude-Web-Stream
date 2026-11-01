"""Helpers to format OpenAI-compatible SSE chunks."""

from __future__ import annotations

import json
import time


def chunk(content=None, reasoning=None, tool_calls=None, finish_reason=None, usage=None, model=None, cid=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning"] = reasoning
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    out = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        out["usage"] = usage
    return "data: " + json.dumps(out) + "\n\n"


DONE = "data: [DONE]\n\n"