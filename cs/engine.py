"""Provider adapter and run engine.

A run is owned by the gateway, not by the HTTP request that created it. If the client disconnects,
the run keeps going and can be re-attached later with Last-Event-ID. Every upstream event is persisted
as an encrypted row before it is announced to subscribers, so replay and live delivery read the same log.
Nothing is retried after output has been delivered, to avoid duplicate generation and charges.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import httpx

from . import security

log = logging.getLogger("claude-stream.engine")

ANTHROPIC_VERSION = "2023-06-01"
HEARTBEAT_SECONDS = 15
MODEL_CACHE_SECONDS = 300
LIVE_STATES = ("created", "queued", "connecting", "streaming")
TERMINAL_STATES = ("completed", "failed", "cancelled")
RETRYABLE_STATUS = {429, 500, 502, 503, 529}
SKIPPED_EVENTS = {"ping"}


class UpstreamError(Exception):
    def __init__(self, status: int, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class Signal:
    """Monotonic version plus condition variable: wakes live subscribers when new events are persisted."""

    def __init__(self):
        self.version = 0
        self._cond = asyncio.Condition()

    async def bump(self) -> None:
        async with self._cond:
            self.version += 1
            self._cond.notify_all()

    async def wait_change(self, seen: int, timeout: float) -> bool:
        async with self._cond:
            try:
                await asyncio.wait_for(self._cond.wait_for(lambda: self.version != seen), timeout)
                return True
            except asyncio.TimeoutError:
                return False


class Accumulator:
    """Rebuilds the final Anthropic message from streamed events, for storage and non-streaming replies."""

    def __init__(self):
        self.message: dict = {}
        self.usage: dict = {}
        self.stop_reason = None
        self._blocks: dict[int, dict] = {}
        self._json: dict[int, list[str]] = {}

    def feed(self, etype: str, p: dict) -> None:
        if etype == "message_start":
            self.message = dict(p.get("message") or {})
            self.usage.update(self.message.get("usage") or {})
        elif etype == "content_block_start":
            i = p.get("index", len(self._blocks))
            block = dict(p.get("content_block") or {})
            if block.get("type") in ("tool_use", "server_tool_use"):
                self._json[i] = []
            self._blocks[i] = block
        elif etype == "content_block_delta":
            i = p.get("index", 0)
            d = p.get("delta") or {}
            block = self._blocks.setdefault(i, {"type": "text", "text": ""})
            kind = d.get("type")
            if kind == "text_delta":
                block["text"] = block.get("text", "") + d.get("text", "")
            elif kind == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + d.get("thinking", "")
            elif kind == "signature_delta":
                block["signature"] = d.get("signature", "")
            elif kind == "input_json_delta":
                self._json.setdefault(i, []).append(d.get("partial_json", ""))
        elif etype == "content_block_stop":
            i = p.get("index", -1)
            if i in self._json:
                raw = "".join(self._json.pop(i))
                try:
                    self._blocks[i]["input"] = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    self._blocks[i]["input_raw"] = raw
        elif etype == "message_delta":
            self.stop_reason = (p.get("delta") or {}).get("stop_reason", self.stop_reason)
            self.usage.update(p.get("usage") or {})

    def result(self) -> dict:
        content = [self._blocks[i] for i in sorted(self._blocks)]
        return {**self.message, "content": content, "stop_reason": self.stop_reason, "usage": self.usage}


def _dump(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _decode(raw: str, etype: str) -> dict:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {"type": etype, "raw": raw}
    if isinstance(obj, dict):
        obj.setdefault("type", etype)
        return obj
    return {"type": etype, "data": obj}


def _error_message(body: bytes) -> str:
    try:
        obj = json.loads(body)
        return str(obj["error"]["message"])
    except Exception:
        return body.decode("utf-8", "replace")[:500]


def _backoff(attempt: int, retry_after: str | None) -> float:
    try:
        return min(float(retry_after), 30.0)
    except (TypeError, ValueError):
        return min(2.0 ** attempt, 8.0)


def _frame(seq: int, kind: str, obj) -> str:
    return f"id: {seq}\nevent: {kind}\ndata: {_dump(obj).decode()}\n\n"


async def iter_sse(resp: httpx.Response):
    """Minimal SSE parser over an upstream response. Yields (event_name, raw_data) pairs."""
    event, data = None, []
    async for line in resp.aiter_lines():
        if line == "":
            if data:
                yield (event or "message"), "\n".join(data)
            event, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        yield (event or "message"), "\n".join(data)


class Engine:
    def __init__(self, store, vault, *, upstream: str, max_streams: int, per_user: int, read_timeout: float):
        self.store = store
        self.vault = vault
        self.upstream = upstream.rstrip("/")
        self._global = asyncio.Semaphore(max_streams)
        self._per_user = per_user
        self._user_sems: dict[str, asyncio.Semaphore] = {}
        self._tasks: dict[str, tuple[str, asyncio.Task]] = {}
        self._signals: dict[str, Signal] = {}
        self._models: dict[str, tuple[float, list]] = {}
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=read_timeout, write=60.0, pool=15.0),
            limits=httpx.Limits(max_connections=max_streams * 2, max_keepalive_connections=max_streams),
        )

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _headers(key: str, stream: bool = False) -> dict:
        return {
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
        }

    def _user_sem(self, user_id: str) -> asyncio.Semaphore:
        if user_id not in self._user_sems:
            self._user_sems[user_id] = asyncio.Semaphore(self._per_user)
        return self._user_sems[user_id]

    async def _set(self, run_id: str, status: str) -> None:
        await self.store.update_run(run_id, {"status": status, "updated_at": time.time()})

    # ---- public API --------------------------------------------------------
    async def start_run(self, *, user_id: str, master: bytes, upstream_key: str, body: dict,
                        conversation_id: str | None, parent_id: str | None,
                        api_key_id: str | None = None, idempotency_key: str | None = None) -> str:
        run_id = "run_" + uuid.uuid4().hex
        now = time.time()
        await self.store.insert_run({
            "id": run_id, "user_id": user_id, "conversation_id": conversation_id,
            "api_key_id": api_key_id, "model": str(body.get("model")), "status": "queued",
            "idempotency_key": idempotency_key, "created_at": now, "updated_at": now,
        })
        self._signals[run_id] = Signal()
        task = asyncio.create_task(
            self._execute(run_id, user_id, master, upstream_key, dict(body), conversation_id, parent_id))
        self._tasks[run_id] = (user_id, task)
        return run_id

    async def subscribe(self, run_id: str, user_id: str, master: bytes, after: int = 0):
        """Replays persisted events after `after`, then follows live ones until the run is terminal."""
        cursor = max(0, int(after))
        while True:
            run = await self.store.get_run(user_id, run_id)
            if run is None:
                return
            done = run["status"] in TERMINAL_STATES
            sig = self._signals.get(run_id)
            seen = sig.version if sig is not None else 0
            rows = await self.store.events_after(run_id, cursor, 200)
            for row in rows:
                cursor = row["seq"]
                try:
                    obj = json.loads(security.open_(master, "events", row["blob"], f"{run_id}:{cursor}".encode()))
                except security.CorruptCiphertext:
                    yield _frame(cursor, "gateway.error", {"error": "stored event failed integrity check"})
                    return
                yield _frame(cursor, row["kind"], obj)
            if rows:
                continue
            if done:
                return
            if sig is not None:
                if await sig.wait_change(seen, HEARTBEAT_SECONDS):
                    continue
            else:
                await asyncio.sleep(HEARTBEAT_SECONDS)
            yield ": heartbeat\n\n"

    async def wait_result(self, run_id: str, user_id: str, master: bytes):
        entry = self._tasks.get(run_id)
        if entry is not None:
            await asyncio.wait({entry[1]})
        run = await self.store.get_run(user_id, run_id)
        if run is None:
            return None, "run not found"
        if run["status"] != "completed" or run["result_blob"] is None:
            return None, run["error"] or f"run ended with status {run['status']}"
        raw = security.open_(master, "results", run["result_blob"], run_id.encode())
        return json.loads(raw), None

    def cancel(self, run_id: str, user_id: str) -> bool:
        entry = self._tasks.get(run_id)
        if entry is None or entry[0] != user_id:
            return False
        entry[1].cancel()
        return True

    def cancel_user(self, user_id: str) -> None:
        for uid, task in list(self._tasks.values()):
            if uid == user_id:
                task.cancel()

    def active_for(self, user_id: str) -> int:
        return sum(1 for uid, _ in self._tasks.values() if uid == user_id)

    def active_count(self) -> int:
        return len(self._tasks)

    async def list_models(self, user_id: str, upstream_key: str, refresh: bool = False) -> list:
        cached = self._models.get(user_id)
        if cached and not refresh and time.time() - cached[0] < MODEL_CACHE_SECONDS:
            return cached[1]
        models: list = []
        after = None
        for _ in range(20):
            params = {"limit": 100}
            if after:
                params["after_id"] = after
            resp = await self._http.get(f"{self.upstream}/v1/models", params=params,
                                        headers=self._headers(upstream_key))
            if resp.status_code >= 400:
                raise UpstreamError(resp.status_code, _error_message(resp.content))
            data = resp.json()
            models.extend(data.get("data", []))
            if not data.get("has_more") or not data.get("last_id"):
                break
            after = data["last_id"]
        self._models[user_id] = (time.time(), models)
        return models

    async def shutdown(self) -> None:
        tasks = [task for _, task in self._tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._http.aclose()

    # ---- run lifecycle -----------------------------------------------------
    async def _execute(self, run_id, user_id, master, upstream_key, body, conversation_id, parent_id):
        sig = self._signals[run_id]
        acc = Accumulator()
        seq = 0
        status, error = "failed", None

        async def emit(kind: str, obj: dict) -> None:
            nonlocal seq
            seq += 1
            blob = security.seal(master, "events", _dump(obj), f"{run_id}:{seq}".encode())
            await self.store.insert_event(run_id, seq, kind, blob)
            await sig.bump()

        try:
            payload = {**body, "stream": True}
            async with self._user_sem(user_id), self._global:
                await self._set(run_id, "connecting")
                attempt, finished = 0, False
                while True:
                    try:
                        async with self._http.stream(
                            "POST", f"{self.upstream}/v1/messages", json=payload,
                            headers=self._headers(upstream_key, stream=True),
                        ) as resp:
                            if resp.status_code >= 400:
                                raise UpstreamError(resp.status_code, _error_message(await resp.aread()),
                                                    resp.headers.get("retry-after"))
                            await self._set(run_id, "streaming")
                            async for etype, raw in iter_sse(resp):
                                if etype in SKIPPED_EVENTS:
                                    continue
                                obj = _decode(raw, etype)
                                acc.feed(etype, obj)
                                await emit(etype, obj)
                                if etype == "message_stop":
                                    finished = True
                                    break
                        break
                    except (httpx.TransportError, UpstreamError) as exc:
                        # Retry only if nothing reached the client yet, so no output is duplicated.
                        retryable = seq == 0 and (
                            isinstance(exc, httpx.TransportError) or exc.status in RETRYABLE_STATUS)
                        if not retryable or attempt >= 2:
                            raise
                        attempt += 1
                        await asyncio.sleep(_backoff(attempt, getattr(exc, "retry_after", None)))
                if not finished:
                    raise UpstreamError(502, "upstream stream ended before message_stop")
            await self._record_success(run_id, user_id, master, conversation_id, parent_id, body, acc.result())
            status = "completed"
        except asyncio.CancelledError:
            status, error = "cancelled", "cancelled"
        except UpstreamError as exc:
            error = f"upstream returned {exc.status}: {exc.message}"
        except httpx.HTTPError as exc:
            error = f"upstream connection failed: {type(exc).__name__}"
        except Exception as exc:  # the gateway stays up; the run records the failure
            log.exception("run %s crashed", run_id)
            error = f"gateway error: {type(exc).__name__}"
        finally:
            try:
                if status == "cancelled":
                    await emit("gateway.cancelled", {"error": error})
                elif status == "failed":
                    await emit("gateway.error", {"error": error})
                await self._finish(run_id, status, error, acc)
            except Exception:
                log.exception("could not finalise run %s", run_id)
            await sig.bump()
            self._tasks.pop(run_id, None)
            self._signals.pop(run_id, None)

    async def _record_success(self, run_id, user_id, master, conversation_id, parent_id, body, message):
        now = time.time()
        if conversation_id:
            mid = "msg_" + uuid.uuid4().hex
            payload = {
                "role": "assistant",
                "content": message.get("content", []),
                "model": message.get("model"),
                "effort": (body.get("output_config") or {}).get("effort"),
                "stop_reason": message.get("stop_reason"),
                "usage": message.get("usage", {}),
            }
            await self.store.insert_message({
                "id": mid, "user_id": user_id, "conversation_id": conversation_id, "parent_id": parent_id,
                "run_id": run_id, "role": "assistant",
                "blob": security.seal(master, "messages", _dump(payload), mid.encode()),
                "created_at": now,
            })
            await self.store.touch_conversation(conversation_id, now)
        await self.store.update_run(run_id, {
            "result_blob": security.seal(master, "results", _dump(message), run_id.encode()),
        })

    async def _finish(self, run_id, status, error, acc: Accumulator) -> None:
        now = time.time()
        usage = acc.usage or {}
        await self.store.update_run(run_id, {
            "status": status, "error": error, "updated_at": now, "completed_at": now,
            "input_tokens": _int(usage.get("input_tokens")),
            "output_tokens": _int(usage.get("output_tokens")),
        })
