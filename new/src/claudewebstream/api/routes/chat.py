from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.middleware import current_user_from_api_key, get_db
from ...claude.client import ClaudeWebClient
from ...claude.models import normalize_effort, normalize_model, efforts_from_models
from ...crypto.cipher import encrypt
from ...crypto.secrets import get_server_key
from ...db.models import ApiKey, Conversation, Message, Stream, StreamChunk
from ...services.claude_sessions import load_runtime_sessions
from ...streaming.manager import StreamManager
from ...streaming.openai_compat import chunk, DONE

router = APIRouter(prefix="/v1", tags=["chat"])

_manager: StreamManager | None = None


def set_manager(m: StreamManager) -> None:
    global _manager
    _manager = m


class ChatMessage(BaseModel):
    role: str
    content: object = ""


class ChatIn(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = []
    stream: bool = True
    effort: str | None = None
    conversation_id: str | None = None
    new_conversation: bool = False
    session_index: int | None = None


async def _get_conversation(db: AsyncSession, user_id: str, cid: str | None) -> Conversation | None:
    if not cid:
        return None
    res = await db.execute(select(Conversation).where(Conversation.id == cid, Conversation.user_id == user_id))
    return res.scalar_one_or_none()


@router.post("/chat/completions")
async def chat_completions(body: ChatIn, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    user_id = api_key.user_id
    sessions = await load_runtime_sessions(db, user_id)
    if not sessions:
        raise HTTPException(400, "No claude.ai session linked. Run `cws claude-login`.")
    idx = body.session_index if body.session_index is not None else 0
    idx = max(0, min(idx, len(sessions) - 1))
    # rotate through available sessions if the chosen one is rate-limited
    order = list(range(idx, len(sessions))) + list(range(0, idx))
    sess_rec, cookies, device_id = sessions[idx]
    org_id = sess_rec.org_id

    # extract prompt early
    user_msgs = [m for m in body.messages if m.role == "user"]
    prompt = user_msgs[-1].content if user_msgs else ""
    if isinstance(prompt, list):
        prompt = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in prompt)
    prompt = str(prompt)[:20000]

    # resolve conversation
    conv = await _get_conversation(db, user_id, body.conversation_id)
    if conv is None or body.new_conversation:
        conv = Conversation(user_id=user_id, claude_org_id=org_id, title=prompt[:60], model=normalize_model(body.model), effort=normalize_effort(body.effort))
        db.add(conv)
        await db.flush()
        # create claude conversation, rotating sessions on 429
        claude_conv_id = None
        last_err = None
        for i in order:
            sess_rec, cookies, device_id = sessions[i]
            org_id = sess_rec.org_id
            try:
                async with ClaudeWebClient(cookies, device_id) as client:
                    claude_conv_id = await client.create_conversation(org_id)
                break
            except Exception as e:
                last_err = e
                if "429" in str(e):
                    continue
                raise HTTPException(502, f"Upstream claude.ai error: {e}")
        if claude_conv_id is None:
            raise HTTPException(429, f"All claude.ai sessions rate-limited: {last_err}")
        conv.claude_org_id = org_id
        conv.claude_conv_id = claude_conv_id
        await db.commit()

    # fetch models/efforts dynamically from claude.ai
    available_models = []
    async with ClaudeWebClient(cookies, device_id) as client:
        try:
            available_models = await client.list_models(org_id)
        except Exception:
            available_models = []
    model = normalize_model(body.model or conv.model, available_models)
    effort = normalize_effort(body.effort or conv.effort)
    thinking_mode = "auto"
    for m in available_models:
        if m.get("id") == model:
            tm = m.get("thinking_modes") or []
            if tm and isinstance(tm[0], dict):
                thinking_mode = tm[0].get("id", thinking_mode)
            break
    # derive thinking mode from model config
    thinking_mode = "auto"
    for m in available_models:
        if m.get("id") == model:
            tm = m.get("thinking_modes") or []
            if tm and isinstance(tm[0], dict):
                thinking_mode = tm[0].get("id", thinking_mode)
            break
    conv.model = model
    conv.effort = effort
    db.add(Message(conversation_id=conv.id, role="user", content=prompt))
    await db.commit()

    # last user message
    user_msgs = [m for m in body.messages if m.role == "user"]
    prompt = user_msgs[-1].content if user_msgs else ""
    if isinstance(prompt, list):
        prompt = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in prompt)
    prompt = str(prompt)[:20000]

    # payload
    payload = {
        "prompt": prompt,
        "timezone": "UTC",
        "locale": "en-US",
        "model": model,
        "effort": effort,
        "thinking_mode": thinking_mode,
        "rendering_mode": "messages",
        "personalized_styles": [{"type": "default", "key": "Default", "name": "Normal"}],
        "tools": [{"type": "web_search_v0", "name": "web_search"}, {"type": "artifacts_v0", "name": "artifacts"}],
        "attachments": [],
        "files": [],
    }
    parent = None
    # try to set parent from last assistant message? skip for now

    stream_rec = Stream(conversation_id=conv.id, api_key_id=api_key.id, status="running")
    db.add(stream_rec)
    await db.commit()
    stream_id = stream_rec.id

    active = await _manager.acquire(stream_id, conv.id, user_id) if _manager else None
    if active is None:
        raise HTTPException(429, "Too many concurrent streams")

    full_text_box = {"text": "", "thinking": "", "tools": []}
    async def event_gen():
        nonlocal full_text_box
        full_text = ""
        thinking = ""
        tool_calls = []
        seq = 0
        try:
            async with ClaudeWebClient(cookies, device_id) as client:
                async for ev in client.send(org_id, conv.claude_conv_id, payload):
                    data = ev.get("data", {})
                    etype = ev.get("event") or data.get("type")
                    if etype == "content_block_delta":
                        delta = data.get("delta", {})
                        dtype = delta.get("type")
                        if dtype == "thinking_delta":
                            t = delta.get("thinking", "")
                            thinking += t
                            yield chunk(reasoning=t, model=model, cid=stream_id)
                            await _persist_chunk(db, stream_id, seq, "thinking", {"text": t}); seq += 1
                        elif dtype == "thinking_summary_delta":
                            sm = delta.get("summary", {})
                            t = sm.get("summary", "") if isinstance(sm, dict) else str(sm)
                            thinking += t
                            yield chunk(reasoning=t, model=model, cid=stream_id)
                            await _persist_chunk(db, stream_id, seq, "thinking", {"text": t}); seq += 1
                        elif dtype == "text_delta":
                            t = delta.get("text", "")
                            full_text += t
                            yield chunk(content=t, model=model, cid=stream_id)
                            await _persist_chunk(db, stream_id, seq, "text", {"text": t}); seq += 1
                        elif dtype == "input_json_delta":
                            # tool input streaming
                            pass
                    elif etype == "content_block_start":
                        cb = data.get("content_block", {})
                        if cb.get("type") == "tool_use":
                            tool_calls.append({"id": cb.get("id"), "name": cb.get("name")})
                            yield chunk(tool_calls=[{"index": len(tool_calls)-1, "id": cb.get("id"), "type": "function", "function": {"name": cb.get("name"), "arguments": ""}}], model=model, cid=stream_id)
                            await _persist_chunk(db, stream_id, seq, "tool_call", cb); seq += 1
                    elif etype in ("message_delta",):
                        usage = data.get("usage")
                        if usage:
                            yield chunk(finish_reason=None, usage={"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)}, model=model, cid=stream_id)
                    elif etype in ("message_stop",):
                        break
            # extract and store artifacts
            try:
                from ...claude.artifacts import extract_code_blocks, save_artifact
                from ...db.models import Artifact
                for i, (lang, code) in enumerate(extract_code_blocks(full_text)):
                    ext = {"python": "py", "js": "js", "javascript": "js", "ts": "ts", "typescript": "ts", "bash": "sh", "json": "json", "html": "html", "css": "css"}.get(lang.lower(), "txt")
                    fname = f"artifact_{i}.{ext}"
                    path, idx_path = save_artifact(conv.id, "code", fname, code.encode())
                    sha = __import__("hashlib").sha256(code.encode()).hexdigest()
                    db.add(Artifact(conversation_id=conv.id, message_id=None, kind="code", filename=fname, mime="text/plain", size=len(code), sha256=sha, storage_path=path, index_path=idx_path))
            except Exception:
                pass
            full_text_box["text"] = full_text
            full_text_box["thinking"] = thinking
            full_text_box["tools"] = tool_calls
            # save message
            msg = Message(conversation_id=conv.id, role="assistant", content=full_text, thinking=thinking, tool_calls_json=json.dumps(tool_calls))
            db.add(msg)
            # update stream
            stream_rec.status = "done"
            from datetime import datetime, timezone
            stream_rec.ended_at = datetime.now(timezone.utc)
            stream_rec.chunk_count = seq
            await db.commit()
            yield chunk(finish_reason="stop", model=model, cid=stream_id)
            yield DONE
        except Exception as e:
            stream_rec.status = "error"
            await db.commit()
            yield chunk(content=f"\n[error] {e}", model=model, cid=stream_id)
            yield DONE
        finally:
            if _manager:
                await _manager.release(stream_id)

    async def _persist_chunk(db, stream_id, seq, kind, payload):
        ch = StreamChunk(stream_id=stream_id, seq=seq, kind=kind, payload_enc=encrypt(json.dumps(payload).encode(), get_server_key()))
        db.add(ch)
        await db.commit()

    if not body.stream:
        async for _ in event_gen():
            pass
        return {"choices": [{"message": {"role": "assistant", "content": full_text_box["text"]}}]}

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={"X-Conversation-Id": conv.id})


@router.get("/models")
async def models():
    from ...claude.models import MODELS
    return {"data": [{"id": m["id"], "object": "model"} for m in MODELS]}