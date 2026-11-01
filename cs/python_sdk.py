"""Claude Stream Python client. Requires httpx (pip install httpx).

    from python_sdk import ClaudeStream

    with ClaudeStream("csk_...", base_url="http://127.0.0.1:8787") as cs:
        for ev in cs.stream(model="MODEL_ID", messages=[{"role": "user", "content": "Hello"}]):
            if ev.text:
                print(ev.text, end="", flush=True)
        print("\\nrun:", cs.last_run_id)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import httpx

ANTHROPIC_VERSION = "2023-06-01"


class ClaudeStreamError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    id: str | None = None

    @property
    def text(self) -> str | None:
        d = self.data.get("delta") or {}
        if self.type == "content_block_delta" and d.get("type") == "text_delta":
            return d.get("text")
        return None

    @property
    def thinking(self) -> str | None:
        d = self.data.get("delta") or {}
        if self.type == "content_block_delta" and d.get("type") == "thinking_delta":
            return d.get("thinking")
        return None

    @property
    def tool_name(self) -> str | None:
        block = self.data.get("content_block") or {}
        if self.type == "content_block_start" and block.get("type") in ("tool_use", "server_tool_use"):
            return block.get("name")
        return None

    @property
    def is_error(self) -> bool:
        return self.type in ("error", "gateway.error")


def parse_sse(lines: Iterator[str]) -> Iterator[Event]:
    event, ident, data = None, None, []
    for line in lines:
        if line == "":
            if data:
                yield Event(event or "message", json.loads("\n".join(data)), ident)
            event, ident, data = None, None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("id:"):
            ident = line[3:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield Event(event or "message", json.loads("\n".join(data)), ident)


class ClaudeStream:
    def __init__(self, api_key: str, base_url: str = "http://127.0.0.1:8787", timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.last_run_id: str | None = None
        self.last_conversation_id: str | None = None
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            try:
                message = resp.json()["error"]["message"]
            except Exception:
                message = resp.text[:300]
            raise ClaudeStreamError(resp.status_code, message)

    @staticmethod
    def _body(model, messages, max_tokens, effort, conversation_id, extra) -> dict:
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, **(extra or {})}
        if effort:
            body["output_config"] = {"effort": effort}
        if conversation_id:
            body["conversation_id"] = conversation_id
        return body

    @staticmethod
    def _headers(idempotency_key: str | None) -> dict:
        return {"idempotency-key": idempotency_key} if idempotency_key else {}

    # ---- messages ----------------------------------------------------------
    def create(self, *, model: str, messages: list, max_tokens: int = 1024, effort: str | None = None,
               conversation_id: str | None = None, idempotency_key: str | None = None,
               extra: dict | None = None) -> dict:
        """Non-streaming call. Returns the final Anthropic message object."""
        body = self._body(model, messages, max_tokens, effort, conversation_id, extra)
        resp = self._http.post("/v1/messages", json=body, headers=self._headers(idempotency_key))
        self._check(resp)
        self.last_run_id = resp.headers.get("x-run-id")
        self.last_conversation_id = resp.headers.get("x-conversation-id")
        return resp.json()

    def stream(self, *, model: str, messages: list, max_tokens: int = 1024, effort: str | None = None,
               conversation_id: str | None = None, idempotency_key: str | None = None,
               extra: dict | None = None) -> Iterator[Event]:
        """Streaming call. Yields Event objects as the upstream produces them."""
        body = self._body(model, messages, max_tokens, effort, conversation_id, extra)
        body["stream"] = True
        with self._http.stream("POST", "/v1/messages", json=body,
                               headers=self._headers(idempotency_key)) as resp:
            if resp.status_code >= 400:
                resp.read()
                self._check(resp)
            self.last_run_id = resp.headers.get("x-run-id")
            self.last_conversation_id = resp.headers.get("x-conversation-id")
            yield from parse_sse(resp.iter_lines())

    def replay(self, run_id: str, after: int = 0) -> Iterator[Event]:
        """Re-attach to a run. Events after `after` (an event id) are replayed, then live ones follow."""
        with self._http.stream("GET", f"/api/runs/{run_id}/events",
                               headers={"last-event-id": str(after)}) as resp:
            if resp.status_code >= 400:
                resp.read()
                self._check(resp)
            yield from parse_sse(resp.iter_lines())

    def cancel(self, run_id: str) -> dict:
        resp = self._http.post(f"/api/runs/{run_id}/cancel")
        self._check(resp)
        return resp.json()

    # ---- catalogue, conversations, artifacts ---------------------------------
    def models(self) -> list[dict]:
        resp = self._http.get("/v1/models")
        self._check(resp)
        return resp.json()["data"]

    def run(self, run_id: str) -> dict:
        resp = self._http.get(f"/api/runs/{run_id}")
        self._check(resp)
        return resp.json()

    def conversations(self) -> list[dict]:
        resp = self._http.get("/api/conversations")
        self._check(resp)
        return resp.json()["data"]

    def conversation(self, conversation_id: str) -> dict:
        resp = self._http.get(f"/api/conversations/{conversation_id}")
        self._check(resp)
        return resp.json()

    def upload_artifact(self, path: str | Path, conversation_id: str | None = None) -> dict:
        p = Path(path)
        params = {"conversation_id": conversation_id} if conversation_id else {}
        resp = self._http.post("/api/artifacts", params=params, content=p.read_bytes(),
                               headers={"content-type": "application/octet-stream",
                                        "x-filename": quote(p.name)})
        self._check(resp)
        return resp.json()

    def download_artifact(self, artifact_id: str) -> bytes:
        resp = self._http.get(f"/api/artifacts/{artifact_id}/content")
        self._check(resp)
        return resp.content


if __name__ == "__main__":
    import os

    key = os.environ.get("CS_API_KEY", "")
    model = os.environ.get("CS_MODEL", "")
    if not key or not model:
        raise SystemExit("set CS_API_KEY and CS_MODEL to try the client")
    with ClaudeStream(key) as cs:
        for event in cs.stream(model=model, max_tokens=256,
                               messages=[{"role": "user", "content": "Say hello in five words."}]):
            if event.text:
                print(event.text, end="", flush=True)
            if event.is_error:
                print("\nerror:", event.data)
        print(f"\nrun {cs.last_run_id}")
