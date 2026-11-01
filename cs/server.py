"""Claude Stream gateway: FastAPI application, account and key routes, the Anthropic
Messages-compatible endpoint, conversations, artifacts, the dashboard, and the `cs` CLI.

Run:   cs start            (or: python -m app.server start)
"""
import argparse
import asyncio
import atexit
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import unquote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__, security
from .engine import LIVE_STATES, Engine, UpstreamError
from .storage import Store

log = logging.getLogger("claude-stream")

APP_NAME = "Claude Stream"
HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CS_DATA_DIR") or Path.home() / ".claude-stream")
UPSTREAM = os.environ.get("CS_UPSTREAM_URL", "https://api.anthropic.com")
MAX_STREAMS = int(os.environ.get("CS_MAX_STREAMS", "8"))
PER_USER_STREAMS = int(os.environ.get("CS_PER_USER_STREAMS", "3"))
UPSTREAM_READ_TIMEOUT = float(os.environ.get("CS_UPSTREAM_READ_TIMEOUT", "600"))
RATE_PER_MINUTE = int(os.environ.get("CS_RATE_PER_MINUTE", "120"))
DEFAULT_PORT = int(os.environ.get("CS_PORT", "8787"))
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 10 * 1024 * 1024
MAX_ARTIFACT = 25 * 1024 * 1024
ZIP_MAX_ENTRIES = 5000
ZIP_MAX_UNCOMPRESSED = 200 * 1024 * 1024
ALL_SCOPES = {"messages", "read", "artifacts"}
COOKIE = "cs_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,40}$")

STORE: Store = None
VAULT: security.Vault = None
ENGINE: Engine = None
RATE: "RateLimiter" = None
LOGIN_RATE: "RateLimiter" = None


class RateLimiter:
    def __init__(self, per_minute: int):
        self.limit = per_minute
        self._hits: dict[str, deque] = {}

    def hit(self, key: str) -> None:
        now = time.monotonic()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.limit:
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
        q.append(now)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global STORE, VAULT, ENGINE, RATE, LOGIN_RATE
    STORE = Store(DATA_DIR / "gateway.db")
    await STORE.open()
    await STORE.mark_interrupted()
    VAULT = security.Vault()
    ENGINE = Engine(STORE, VAULT, upstream=UPSTREAM, max_streams=MAX_STREAMS,
                    per_user=PER_USER_STREAMS, read_timeout=UPSTREAM_READ_TIMEOUT)
    RATE = RateLimiter(RATE_PER_MINUTE)
    LOGIN_RATE = RateLimiter(10)
    log.info("%s %s ready (data: %s)", APP_NAME, __version__, DATA_DIR)
    try:
        yield
    finally:
        await ENGINE.shutdown()
        await STORE.close()


app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
    return response


@app.exception_handler(UpstreamError)
async def _upstream_error(_request, exc: UpstreamError):
    status = exc.status if exc.status in (400, 404, 413, 422, 429) else 502
    return JSONResponse({"type": "error", "error": {"type": "upstream_error", "message": exc.message}},
                        status_code=status)


@app.exception_handler(security.CorruptCiphertext)
async def _corrupt(_request, _exc):
    return JSONResponse(
        {"type": "error", "error": {"type": "data_integrity",
                                    "message": "stored data failed authentication; it may be corrupted "
                                               "or the vault was unlocked with the wrong password"}},
        status_code=500)


# ---- principals and guards --------------------------------------------------

@dataclass
class Principal:
    user_id: str
    via: str                      # "session" (dashboard cookie) or "apikey"
    scopes: set
    master: Optional[bytes]       # None while the vault is locked
    api_key_id: Optional[str] = None

    @property
    def locked(self) -> bool:
        return self.master is None


def _bearer(value: Optional[str]) -> Optional[str]:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


async def principal(request: Request) -> Principal:
    token = request.cookies.get(COOKIE)
    if token:
        sess = VAULT.session(token)
        if sess is None:
            raise HTTPException(401, "session missing or expired; sign in again")
        if request.method not in SAFE_METHODS and not hmac.compare_digest(
                request.headers.get("x-csrf-token", ""), sess.csrf):
            raise HTTPException(403, "CSRF token missing or invalid")
        p = Principal(sess.user_id, "session", set(ALL_SCOPES), VAULT.master(sess.user_id))
    else:
        raw = request.headers.get("x-api-key") or _bearer(request.headers.get("authorization"))
        parsed = security.parse_gateway_key(raw) if raw else None
        if parsed is None:
            raise HTTPException(401, "missing or malformed credentials")
        row = await STORE.get_api_key(parsed[0])
        if (row is None or row["revoked_at"] is not None
                or (row["expires_at"] and row["expires_at"] < time.time())
                or not hmac.compare_digest(security.hash_secret(parsed[1]), row["secret_hash"])):
            raise HTTPException(401, "invalid API key")
        await STORE.touch_api_key(row["id"])
        p = Principal(row["user_id"], "apikey", set(row["scopes"].split(",")),
                      VAULT.master(row["user_id"]), row["id"])
    RATE.hit(p.user_id)
    return p


def require_scope(p: Principal, scope: str) -> None:
    if scope not in p.scopes:
        raise HTTPException(403, f"this API key lacks the '{scope}' scope")


def require_session(p: Principal) -> None:
    if p.via != "session":
        raise HTTPException(403, "this action requires a dashboard session, not an API key")


def require_unlocked(p: Principal) -> None:
    if p.locked:
        raise HTTPException(423, "vault is locked; unlock it in the dashboard first")


async def upstream_key(p: Principal) -> str:
    require_unlocked(p)
    blob = await STORE.get_credential(p.user_id)
    if blob is None:
        raise HTTPException(409, "no Anthropic API key configured; add one in Settings")
    return security.open_(p.master, "credential", blob, p.user_id.encode()).decode()


def parse_scopes(values) -> list:
    scopes = sorted(set(values or []))
    if not scopes or not set(scopes) <= ALL_SCOPES:
        raise ValueError(f"scopes must be a non-empty subset of {sorted(ALL_SCOPES)}")
    return scopes


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _open_title(p: Principal, row) -> str:
    return security.open_(p.master, "titles", row["title_blob"], row["id"].encode()).decode()


def _title_from(body: dict) -> str:
    for m in reversed(body.get("messages", [])):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                text = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
            else:
                text = str(c or "")
            if text.strip():
                return text.strip()[:60]
    return "New conversation"


def _public_key(r) -> dict:
    return {"id": r["id"], "name": r["name"], "scopes": r["scopes"].split(","),
            "created_at": r["created_at"], "expires_at": r["expires_at"],
            "last_used_at": r["last_used_at"], "revoked": r["revoked_at"] is not None}


def _run_public(r) -> dict:
    return {"id": r["id"], "status": r["status"], "live": r["status"] in LIVE_STATES,
            "model": r["model"], "conversation_id": r["conversation_id"], "api_key_id": r["api_key_id"],
            "error": r["error"], "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
            "created_at": r["created_at"], "updated_at": r["updated_at"], "completed_at": r["completed_at"]}


def _artifact_meta(p: Principal, r) -> dict:
    return json.loads(security.open_(p.master, "artifacts", r["meta_blob"], f"{r['id']}:meta".encode()))


async def _own_run(p: Principal, run_id: str):
    run = await STORE.get_run(p.user_id, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


# ---- request models ---------------------------------------------------------

class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=10, max_length=256)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=256)


class PasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class CredentialBody(BaseModel):
    api_key: str = Field(min_length=20, max_length=512)


class KeyBody(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=60)
    scopes: list[str] = Field(default_factory=lambda: ["messages", "read"])
    expires_days: Optional[int] = Field(default=None, ge=1, le=3650)


class ConversationBody(BaseModel):
    title: str = Field(default="New conversation", max_length=120)


# ---- accounts ---------------------------------------------------------------

def _start_session(response: Response, user_id: str, username: str, master: bytes) -> dict:
    token, csrf = VAULT.new_session(user_id)
    VAULT.unlock(user_id, master)
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                        max_age=security.SESSION_TTL, path="/")
    return {"user_id": user_id, "username": username, "csrf": csrf, "unlocked": True}


@app.post("/api/auth/register")
async def register(body: Credentials, response: Response):
    if not USERNAME_RE.match(body.username):
        raise HTTPException(400, "usernames use letters, digits, '.', '_' or '-' (3-40 characters)")
    if await STORE.get_user_by_name(body.username):
        raise HTTPException(409, "that username is taken")
    salt = security.new_salt()
    master = await asyncio.to_thread(security.derive_master_key, body.password, salt)
    user_id = str(uuid.uuid4())
    verifier = security.seal(master, "verifier", security.VERIFIER_PLAINTEXT, user_id.encode())
    try:
        await STORE.create_user(user_id, body.username, salt, verifier)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "that username is taken")
    return _start_session(response, user_id, body.username, master)


@app.post("/api/auth/login")
async def login(body: LoginBody, response: Response):
    LOGIN_RATE.hit(body.username.lower())
    row = await STORE.get_user_by_name(body.username)
    if row is None:
        await asyncio.to_thread(security.derive_master_key, body.password, security.new_salt())
        raise HTTPException(401, "invalid username or password")
    master = await asyncio.to_thread(security.derive_master_key, body.password, row["kdf_salt"])
    if not security.verifies(master, row["id"], row["verifier"]):
        raise HTTPException(401, "invalid username or password")
    return _start_session(response, row["id"], row["username"], master)


@app.post("/api/auth/unlock")
async def unlock(body: PasswordBody, p: Principal = Depends(principal)):
    require_session(p)
    row = await STORE.get_user(p.user_id)
    master = await asyncio.to_thread(security.derive_master_key, body.password, row["kdf_salt"])
    if not security.verifies(master, p.user_id, row["verifier"]):
        raise HTTPException(401, "incorrect password")
    VAULT.unlock(p.user_id, master)
    return {"unlocked": True}


@app.post("/api/auth/lock")
async def lock(p: Principal = Depends(principal)):
    require_session(p)
    ENGINE.cancel_user(p.user_id)   # runs need the key to persist their events
    VAULT.lock(p.user_id)
    return {"unlocked": False}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response, p: Principal = Depends(principal)):
    require_session(p)
    ENGINE.cancel_user(p.user_id)
    VAULT.lock(p.user_id)
    VAULT.drop_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request, p: Principal = Depends(principal)):
    row = await STORE.get_user(p.user_id)
    sess = VAULT.session(request.cookies.get(COOKIE)) if p.via == "session" else None
    return {"user_id": p.user_id, "username": row["username"], "auth": p.via,
            "unlocked": not p.locked, "provider_configured": await STORE.get_credential(p.user_id) is not None,
            "csrf": sess.csrf if sess else None}


# ---- provider credential ----------------------------------------------------

@app.get("/api/credential")
async def get_credential(p: Principal = Depends(principal)):
    require_session(p)
    blob = await STORE.get_credential(p.user_id)
    hint = None
    if blob is not None and not p.locked:
        key = security.open_(p.master, "credential", blob, p.user_id.encode()).decode()
        hint = "..." + key[-4:]
    return {"configured": blob is not None, "hint": hint}


@app.put("/api/credential")
async def put_credential(body: CredentialBody, p: Principal = Depends(principal)):
    require_session(p)
    require_unlocked(p)
    key = body.api_key.strip()
    if not key.startswith("sk-ant-"):
        raise HTTPException(400, "expected an Anthropic API key beginning with sk-ant-")
    await STORE.set_credential(p.user_id, security.seal(p.master, "credential", key.encode(), p.user_id.encode()))
    return {"configured": True, "hint": "..." + key[-4:]}


@app.delete("/api/credential")
async def delete_credential(p: Principal = Depends(principal)):
    require_session(p)
    await STORE.delete_credential(p.user_id)
    return {"configured": False}


# ---- gateway API keys -------------------------------------------------------

async def _issue_key(user_id: str, name: str, scopes: list, expires_days: Optional[int]) -> tuple[str, str]:
    key_id, full, digest = security.new_gateway_key()
    now = time.time()
    await STORE.insert_api_key({
        "id": key_id, "user_id": user_id, "name": name, "secret_hash": digest,
        "scopes": ",".join(scopes), "created_at": now,
        "expires_at": now + expires_days * 86400 if expires_days else None,
    })
    return key_id, full


@app.get("/api/keys")
async def list_keys(p: Principal = Depends(principal)):
    require_session(p)
    return {"keys": [_public_key(r) for r in await STORE.list_api_keys(p.user_id)]}


@app.post("/api/keys")
async def create_key(body: KeyBody, p: Principal = Depends(principal)):
    require_session(p)
    try:
        scopes = parse_scopes(body.scopes)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    key_id, full = await _issue_key(p.user_id, body.name, scopes, body.expires_days)
    return {"id": key_id, "key": full, "scopes": scopes,
            "notice": "Shown once. The gateway stores only a hash and cannot display it again."}


@app.delete("/api/keys/{key_id}")
async def revoke_key(key_id: str, p: Principal = Depends(principal)):
    require_session(p)
    if not await STORE.revoke_api_key(p.user_id, key_id):
        raise HTTPException(404, "key not found or already revoked")
    return {"revoked": key_id}


@app.post("/api/keys/{key_id}/rotate")
async def rotate_key(key_id: str, p: Principal = Depends(principal)):
    require_session(p)
    row = await STORE.get_api_key(key_id)
    if row is None or row["user_id"] != p.user_id:
        raise HTTPException(404, "key not found")
    await STORE.revoke_api_key(p.user_id, key_id)
    new_id, full = await _issue_key(p.user_id, row["name"], row["scopes"].split(","), None)
    return {"id": new_id, "key": full, "replaces": key_id}


# ---- models and the Messages-compatible endpoint -----------------------------

@app.get("/v1/models")
@app.get("/api/models")
async def models(refresh: bool = False, p: Principal = Depends(principal)):
    require_scope(p, "read")
    key = await upstream_key(p)
    data = await ENGINE.list_models(p.user_id, key, refresh)
    return {"data": data, "has_more": False}


async def _record_user_turn(p: Principal, body: dict, gateway: dict):
    conv_id = gateway.get("conversation_id")
    if conv_id:
        if await STORE.get_conversation(p.user_id, conv_id) is None:
            raise HTTPException(404, "conversation not found")
    else:
        conv_id = "conv_" + uuid.uuid4().hex
        now = time.time()
        await STORE.insert_conversation({
            "id": conv_id, "user_id": p.user_id,
            "title_blob": security.seal(p.master, "titles", _title_from(body).encode(), conv_id.encode()),
            "created_at": now, "updated_at": now,
        })
    last_user = next((m for m in reversed(body["messages"])
                      if isinstance(m, dict) and m.get("role") == "user"), None)
    parent = gateway.get("parent_message_id")
    if last_user is None:
        return conv_id, parent
    if parent:
        if await STORE.get_message(p.user_id, parent) is None:
            raise HTTPException(404, "parent_message_id not found")
    else:
        parent = await STORE.last_message_id(conv_id)
    mid = "msg_" + uuid.uuid4().hex
    blob = security.seal(p.master, "messages",
                         json.dumps({"role": "user", "content": last_user.get("content")}).encode(),
                         mid.encode())
    await STORE.insert_message({"id": mid, "user_id": p.user_id, "conversation_id": conv_id,
                                "parent_id": parent, "run_id": None, "role": "user",
                                "blob": blob, "created_at": time.time()})
    return conv_id, mid


async def _respond(p: Principal, run_id: str, stream: bool, request: Request):
    run = await STORE.get_run(p.user_id, run_id)
    headers = {"X-Run-Id": run_id, "X-Conversation-Id": (run["conversation_id"] or "") if run else ""}
    if stream:
        return StreamingResponse(
            ENGINE.subscribe(run_id, p.user_id, p.master, _last_event_id(request)),
            media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    message, error = await ENGINE.wait_result(run_id, p.user_id, p.master)
    if message is None:
        return JSONResponse({"type": "error", "error": {"type": "api_error", "message": error},
                             "run_id": run_id}, status_code=502, headers=headers)
    return JSONResponse(message, headers=headers)


@app.post("/v1/messages")
async def create_message(request: Request, p: Principal = Depends(principal)):
    require_scope(p, "messages")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "request body too large")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "request body must be JSON")
    if (not isinstance(body, dict) or not isinstance(body.get("model"), str)
            or not isinstance(body.get("messages"), list) or not body["messages"]):
        raise HTTPException(400, "model (string) and messages (non-empty list) are required")
    gateway = {k: body.pop(k) for k in ("conversation_id", "parent_message_id") if k in body}
    stream = bool(body.pop("stream", False))
    require_unlocked(p)

    idem = request.headers.get("idempotency-key")
    if idem:
        existing = await STORE.get_run_by_idem(p.user_id, idem)
        if existing is not None:
            return await _respond(p, existing["id"], stream, request)

    key = await upstream_key(p)
    conv_id, user_msg_id = await _record_user_turn(p, body, gateway)
    run_id = await ENGINE.start_run(user_id=p.user_id, master=p.master, upstream_key=key, body=body,
                                    conversation_id=conv_id, parent_id=user_msg_id,
                                    api_key_id=p.api_key_id, idempotency_key=idem)
    return await _respond(p, run_id, stream, request)


# ---- runs -------------------------------------------------------------------

@app.get("/api/runs")
async def list_runs(limit: int = 50, p: Principal = Depends(principal)):
    require_scope(p, "read")
    rows = await STORE.list_runs(p.user_id, min(max(limit, 1), 200))
    return {"data": [_run_public(r) for r in rows]}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, p: Principal = Depends(principal)):
    require_scope(p, "read")
    run = await _own_run(p, run_id)
    out = _run_public(run)
    if run["result_blob"] is not None and not p.locked:
        out["message"] = json.loads(security.open_(p.master, "results", run["result_blob"], run_id.encode()))
    return out


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, p: Principal = Depends(principal)):
    require_scope(p, "read")
    require_unlocked(p)
    await _own_run(p, run_id)
    return StreamingResponse(
        ENGINE.subscribe(run_id, p.user_id, p.master, _last_event_id(request)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-Id": run_id})


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, p: Principal = Depends(principal)):
    require_scope(p, "messages")
    await _own_run(p, run_id)
    return {"run_id": run_id, "cancel_requested": ENGINE.cancel(run_id, p.user_id)}


# ---- conversations ----------------------------------------------------------

@app.get("/api/conversations")
async def list_conversations(p: Principal = Depends(principal)):
    require_scope(p, "read")
    require_unlocked(p)
    rows = await STORE.list_conversations(p.user_id)
    return {"data": [{"id": r["id"], "title": _open_title(p, r), "created_at": r["created_at"],
                      "updated_at": r["updated_at"]} for r in rows]}


@app.post("/api/conversations")
async def create_conversation(body: ConversationBody, p: Principal = Depends(principal)):
    require_scope(p, "messages")
    require_unlocked(p)
    conv_id = "conv_" + uuid.uuid4().hex
    now = time.time()
    await STORE.insert_conversation({
        "id": conv_id, "user_id": p.user_id,
        "title_blob": security.seal(p.master, "titles", body.title.encode(), conv_id.encode()),
        "created_at": now, "updated_at": now})
    return {"id": conv_id, "title": body.title}


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, p: Principal = Depends(principal)):
    require_scope(p, "read")
    require_unlocked(p)
    conv = await STORE.get_conversation(p.user_id, conversation_id)
    if conv is None:
        raise HTTPException(404, "conversation not found")
    messages = []
    for m in await STORE.list_messages(p.user_id, conversation_id):
        payload = json.loads(security.open_(p.master, "messages", m["blob"], m["id"].encode()))
        messages.append({"id": m["id"], "parent_id": m["parent_id"], "run_id": m["run_id"],
                         "created_at": m["created_at"], **payload})
    return {"id": conv["id"], "title": _open_title(p, conv), "messages": messages}


# ---- artifacts --------------------------------------------------------------

def _inspect_zip(data: bytes) -> list:
    """Indexes a ZIP's manifest only. Nothing is extracted or executed."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(400, "file is not a valid ZIP archive")
    infos = zf.infolist()
    if len(infos) > ZIP_MAX_ENTRIES:
        raise HTTPException(400, f"archive has more than {ZIP_MAX_ENTRIES} entries")
    if sum(i.file_size for i in infos) > ZIP_MAX_UNCOMPRESSED:
        raise HTTPException(400, "archive uncompressed size exceeds the limit")
    index = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in PurePosixPath(name).parts:
            raise HTTPException(400, f"unsafe path inside archive: {info.filename!r}")
        index.append({"path": name, "size": info.file_size, "compressed": info.compress_size,
                      "dir": info.is_dir()})
    return index


@app.post("/api/artifacts")
async def upload_artifact(request: Request, conversation_id: Optional[str] = None,
                          run_id: Optional[str] = None, p: Principal = Depends(principal)):
    require_scope(p, "artifacts")
    require_unlocked(p)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_ARTIFACT:
        raise HTTPException(413, "artifact exceeds the 25 MB limit")
    data = await request.body()
    if len(data) > MAX_ARTIFACT:
        raise HTTPException(413, "artifact exceeds the 25 MB limit")
    filename = unquote(request.headers.get("x-filename") or "upload.bin")
    filename = filename.replace("\\", "/").split("/")[-1][:200] or "upload.bin"
    mime = (request.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
    if conversation_id and await STORE.get_conversation(p.user_id, conversation_id) is None:
        raise HTTPException(404, "conversation not found")
    if run_id:
        await _own_run(p, run_id)
    index = None
    if filename.lower().endswith(".zip") or mime in ("application/zip", "application/x-zip-compressed"):
        index = _inspect_zip(data)
    aid = "art_" + uuid.uuid4().hex
    rel = Path("artifacts") / p.user_id / f"{aid}.bin"
    path = DATA_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(security.seal(p.master, "artifacts", data, aid.encode()))
    meta = security.seal(p.master, "artifacts", json.dumps({"filename": filename, "index": index}).encode(),
                         f"{aid}:meta".encode())
    await STORE.insert_artifact({
        "id": aid, "user_id": p.user_id, "conversation_id": conversation_id, "run_id": run_id,
        "meta_blob": meta, "mime": mime, "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), "path": str(rel).replace("\\", "/"),
        "status": "indexed" if index is not None else "stored", "created_at": time.time()})
    return {"id": aid, "filename": filename, "mime": mime, "size": len(data),
            "status": "indexed" if index is not None else "stored",
            "entries": len(index) if index is not None else None}


@app.get("/api/artifacts")
async def list_artifacts(conversation_id: Optional[str] = None, p: Principal = Depends(principal)):
    require_scope(p, "artifacts")
    require_unlocked(p)
    out = []
    for r in await STORE.list_artifacts(p.user_id, conversation_id):
        meta = _artifact_meta(p, r)
        out.append({"id": r["id"], "filename": meta["filename"], "mime": r["mime"], "size": r["size"],
                    "sha256": r["sha256"], "status": r["status"], "conversation_id": r["conversation_id"],
                    "run_id": r["run_id"], "created_at": r["created_at"],
                    "entries": len(meta["index"]) if meta["index"] is not None else None})
    return {"data": out}


@app.get("/api/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, p: Principal = Depends(principal)):
    require_scope(p, "artifacts")
    require_unlocked(p)
    r = await STORE.get_artifact(p.user_id, artifact_id)
    if r is None:
        raise HTTPException(404, "artifact not found")
    meta = _artifact_meta(p, r)
    return {"id": r["id"], "filename": meta["filename"], "mime": r["mime"], "size": r["size"],
            "sha256": r["sha256"], "status": r["status"], "index": meta["index"],
            "conversation_id": r["conversation_id"], "run_id": r["run_id"], "created_at": r["created_at"]}


@app.get("/api/artifacts/{artifact_id}/content")
async def artifact_content(artifact_id: str, p: Principal = Depends(principal)):
    require_scope(p, "artifacts")
    require_unlocked(p)
    r = await STORE.get_artifact(p.user_id, artifact_id)
    if r is None:
        raise HTTPException(404, "artifact not found")
    data = security.open_(p.master, "artifacts", (DATA_DIR / r["path"]).read_bytes(), artifact_id.encode())
    if hashlib.sha256(data).hexdigest() != r["sha256"]:
        raise HTTPException(500, "artifact digest mismatch")
    safe_name = _artifact_meta(p, r)["filename"].replace('"', "_")
    return Response(content=data, media_type=r["mime"],
                    headers={"Content-Disposition": f'attachment; filename="{safe_name}"'})


# ---- overview, health, dashboard ---------------------------------------------

@app.get("/api/overview")
async def overview(p: Principal = Depends(principal)):
    require_scope(p, "read")
    return {
        "user_id": p.user_id,
        "unlocked": not p.locked,
        "provider_configured": await STORE.get_credential(p.user_id) is not None,
        "active_runs": ENGINE.active_for(p.user_id),
        "runs_by_status": await STORE.count_runs_by_status(p.user_id),
        "usage": await STORE.usage_totals(p.user_id),
        "upstream": UPSTREAM,
        "effort_options": ["low", "medium", "high"],
        "limits": {"max_streams": MAX_STREAMS, "per_user_streams": PER_USER_STREAMS,
                   "rate_per_minute": RATE_PER_MINUTE},
    }


@app.get("/health")
async def health():
    return {"status": "ok", "name": APP_NAME, "version": __version__, "active_runs": ENGINE.active_count()}


@app.get("/")
async def dashboard():
    return FileResponse(HERE / "web" / "index.html", media_type="text/html")


# ---- CLI --------------------------------------------------------------------

def _probe(port: int, host: str = "127.0.0.1") -> bool:
    try:
        return httpx.get(f"http://{host}:{port}/health", timeout=1.5).status_code == 200
    except httpx.HTTPError:
        return False


def _cleanup_lock(lock: Path) -> None:
    lock.unlink(missing_ok=True)


def cmd_start(args) -> None:
    if args.host not in LOOPBACK and not args.allow_remote:
        sys.exit("Refusing to bind to a non-loopback address without --allow-remote.")
    import uvicorn

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = DATA_DIR / "cs.lock"
    if lock.exists():
        try:
            recorded = json.loads(lock.read_text()).get("port")
        except (ValueError, OSError):
            recorded = None
        if recorded and _probe(int(recorded)):
            sys.exit(f"Claude Stream is already running on port {recorded} for this data directory.")
        lock.unlink(missing_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "port": args.port}))
    atexit.register(_cleanup_lock, lock)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(f"{APP_NAME} {__version__}")
    print(f"  dashboard : http://{args.host}:{args.port}/")
    print(f"  API base  : http://{args.host}:{args.port}/v1")
    print(f"  data      : {DATA_DIR}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_status(args) -> None:
    if _probe(args.port, args.host):
        info = httpx.get(f"http://{args.host}:{args.port}/health", timeout=2).json()
        print(f"running on http://{args.host}:{args.port}  active runs: {info.get('active_runs')}")
    else:
        print(f"not running on http://{args.host}:{args.port}")


async def _with_store(fn):
    store = Store(DATA_DIR / "gateway.db")
    await store.open()
    try:
        return await fn(store)
    finally:
        await store.close()


def cmd_users(args) -> None:
    async def run(store):
        for r in await store.list_users():
            print(f"{r['username']}\t{r['id']}")
    asyncio.run(_with_store(run))


def cmd_keys(args) -> None:
    async def run(store):
        user = await store.get_user_by_name(args.user)
        if user is None:
            sys.exit(f"no account named {args.user!r}; create one in the dashboard first")
        uid = user["id"]
        if args.action == "create":
            try:
                scopes = parse_scopes(args.scopes.split(","))
            except ValueError as exc:
                sys.exit(str(exc))
            key_id, full, digest = security.new_gateway_key()
            now = time.time()
            await store.insert_api_key({
                "id": key_id, "user_id": uid, "name": args.name, "secret_hash": digest,
                "scopes": ",".join(scopes), "created_at": now,
                "expires_at": now + args.expires_days * 86400 if args.expires_days else None})
            print(f"Created key {key_id} for {args.user} (scopes: {','.join(scopes)})")
            print("Copy it now; it will not be shown again:")
            print(full)
        elif args.action == "list":
            for r in await store.list_api_keys(uid):
                state = "revoked" if r["revoked_at"] else "active"
                print(f"{r['id']}\t{r['name']}\t{r['scopes']}\t{state}")
        elif args.action == "revoke":
            if await store.revoke_api_key(uid, args.key_id):
                print(f"revoked {args.key_id}")
            else:
                sys.exit("no active key with that id for this user")
    asyncio.run(_with_store(run))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="cs", description=f"{APP_NAME} local gateway")
    sub = parser.add_subparsers(dest="cmd")

    s = sub.add_parser("start", help="run the gateway in the foreground")
    s.add_argument("--host", default=os.environ.get("CS_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--allow-remote", action="store_true", help="permit a non-loopback bind (understand the risk)")
    s.set_defaults(fn=cmd_start)

    st = sub.add_parser("status", help="check whether the gateway is running")
    st.add_argument("--host", default="127.0.0.1")
    st.add_argument("--port", type=int, default=DEFAULT_PORT)
    st.set_defaults(fn=cmd_status)

    sub.add_parser("users", help="list accounts (no secrets)").set_defaults(fn=cmd_users)

    k = sub.add_parser("keys", help="manage gateway API keys for an account")
    ka = k.add_subparsers(dest="action", required=True)
    kc = ka.add_parser("create")
    kc.add_argument("--user", required=True)
    kc.add_argument("--name", default="default")
    kc.add_argument("--scopes", default="messages,read", help="comma list of messages,read,artifacts")
    kc.add_argument("--expires-days", type=int, default=None)
    kl = ka.add_parser("list")
    kl.add_argument("--user", required=True)
    kr = ka.add_parser("revoke")
    kr.add_argument("key_id")
    kr.add_argument("--user", required=True)
    k.set_defaults(fn=cmd_keys)

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.cmd is None:
        args = parser.parse_args(["start"])
    args.fn(args)


if __name__ == "__main__":
    main()
