from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.middleware import current_user_from_api_key, get_db
from ...claude.client import ClaudeWebClient
from ...claude.models import normalize_model
from ...services.claude_sessions import load_runtime_sessions
from ...db.models import Conversation, Message

router = APIRouter(prefix="/v1", tags=["messages"])


class Msg(BaseModel):
    role: str
    content: object = ""


class MessagesIn(BaseModel):
    model: str | None = None
    messages: list[Msg] = []
    max_tokens: int | None = None
    stream: bool = False
    effort: str | None = None
    conversation_id: str | None = None


@router.post("/messages")
async def messages(body: MessagesIn, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    user_id = api_key.user_id
    sessions = await load_runtime_sessions(db, user_id)
    if not sessions:
        raise HTTPException(400, "No claude.ai session linked")
    sess_rec, cookies, device_id = sessions[0]
    org_id = sess_rec.org_id

    conv = None
    if body.conversation_id:
        from sqlalchemy import select
        res = await db.execute(select(Conversation).where(Conversation.id == body.conversation_id, Conversation.user_id == user_id))
        conv = res.scalar_one_or_none()
    if conv is None:
        user_msgs = [m for m in body.messages if m.role == "user"]
        prompt0 = user_msgs[-1].content if user_msgs else ""
        if isinstance(prompt0, list):
            prompt0 = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in prompt0)
        conv = Conversation(user_id=user_id, claude_org_id=org_id, title=str(prompt0)[:60], model=normalize_model(body.model))
        db.add(conv)
        await db.flush()

    user_msgs = [m for m in body.messages if m.role == "user"]
    prompt = user_msgs[-1].content if user_msgs else ""
    if isinstance(prompt, list):
        prompt = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in prompt)
    prompt = str(prompt)

    payload = {
        "prompt": prompt,
        "timezone": "UTC",
        "locale": "en-US",
        "model": normalize_model(body.model or conv.model),
        "effort": body.effort or "low",
        "thinking_mode": "auto",
        "rendering_mode": "messages",
        "personalized_styles": [{"type": "default", "key": "Default", "name": "Normal"}],
        "tools": [{"type": "web_search_v0", "name": "web_search"}, {"type": "artifacts_v0", "name": "artifacts"}],
        "attachments": [],
        "files": [],
    }

    msg_id = "msg_" + uuid.uuid4().hex
    if not body.stream:
        # non-stream: collect
        full = ""
        thinking = ""
        async with ClaudeWebClient(cookies, device_id) as client:
            async for ev in client.send(org_id, conv.claude_conv_id, payload):
                d = ev.get("data", {})
                if d.get("type") == "content_block_delta":
                    dt = d.get("delta", {}).get("type")
                    if dt == "text_delta":
                        full += d["delta"].get("text", "")
                    elif dt in ("thinking_delta", "thinking_summary_delta"):
                        sm = d["delta"].get("summary", {})
                        thinking += sm.get("summary", "") if isinstance(sm, dict) else d["delta"].get("thinking", "")
        db.add(Message(conversation_id=conv.id, role="user", content=prompt))
        db.add(Message(conversation_id=conv.id, role="assistant", content=full, thinking=thinking))
        await db.commit()
        return {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": normalize_model(body.model or conv.model),
            "content": [{"type": "text", "text": full}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    async def gen():
        yield "event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "model": normalize_model(body.model or conv.model), "content": []}}) + "\n\n"
        yield "event: content_block_start\ndata: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}) + "\n\n"
        full = ""
        thinking = ""
        async with ClaudeWebClient(cookies, device_id) as client:
            async for ev in client.send(org_id, conv.claude_conv_id, payload):
                d = ev.get("data", {})
                if d.get("type") == "content_block_delta":
                    dt = d.get("delta", {}).get("type")
                    if dt == "text_delta":
                        t = d["delta"].get("text", "")
                        full += t
                        yield "event: content_block_delta\ndata: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}}) + "\n\n"
                    elif dt in ("thinking_delta", "thinking_summary_delta"):
                        sm = d["delta"].get("summary", {})
                        t = sm.get("summary", "") if isinstance(sm, dict) else d["delta"].get("thinking", "")
                        thinking += t
                        yield "event: content_block_delta\ndata: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": t}}) + "\n\n"
        yield "event: content_block_stop\ndata: " + json.dumps({"type": "content_block_stop", "index": 0}) + "\n\n"
        yield "event: message_delta\ndata: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 0}}) + "\n\n"
        yield "event: message_stop\ndata: " + json.dumps({"type": "message_stop"}) + "\n\n"
        db.add(Message(conversation_id=conv.id, role="user", content=prompt))
        db.add(Message(conversation_id=conv.id, role="assistant", content=full, thinking=thinking))
        await db.commit()

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/models")
async def models():
    return {"data": [{"id": "claude-sonnet-4-6", "type": "model", "display_name": "Claude Sonnet 4.6"}]}
