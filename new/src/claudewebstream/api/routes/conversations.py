from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.middleware import current_user_from_api_key, get_db
from ...db.models import Conversation, Message

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("/search")
async def search(q: str = Query(..., min_length=1, max_length=200), api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Message).join(Conversation).where(Conversation.user_id == api_key.user_id))
    out = []
    ql = q.lower()
    for m in res.scalars().all():
        if ql in (m.content or "").lower() or ql in (m.thinking or "").lower():
            out.append({"conversation_id": m.conversation_id, "message_id": m.id, "role": m.role, "snippet": (m.content or "")[:200]})
    return {"results": out[:50]}


@router.get("")
async def list_conversations(api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Conversation).where(Conversation.user_id == api_key.user_id).order_by(Conversation.updated_at.desc()))
    return [{"id": c.id, "title": c.title, "model": c.model, "effort": c.effort, "parent_id": c.parent_id, "created_at": c.created_at} for c in res.scalars().all()]


@router.get("/{conv_id}")
async def get_conversation(conv_id: str, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Conversation).where(Conversation.id == conv_id, Conversation.user_id == api_key.user_id))
    c = res.scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "not found")
    mres = await db.execute(select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at))
    msgs = [{"id": m.id, "role": m.role, "content": m.content, "thinking": m.thinking, "tool_calls": m.tool_calls_json, "created_at": m.created_at} for m in mres.scalars().all()]
    cres = await db.execute(select(Conversation).where(Conversation.parent_id == conv_id))
    children = [ch.id for ch in cres.scalars().all()]
    return {"id": c.id, "title": c.title, "model": c.model, "effort": c.effort, "parent_id": c.parent_id, "children": children, "messages": msgs}


class UpdateConvIn(BaseModel):
    title: str | None = None
    model: str | None = None
    effort: str | None = None


@router.patch("/{conv_id}")
async def update_conversation(conv_id: str, body: UpdateConvIn, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Conversation).where(Conversation.id == conv_id, Conversation.user_id == api_key.user_id))
    c = res.scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "not found")
    if body.title is not None:
        c.title = body.title
    if body.model is not None:
        c.model = body.model
    if body.effort is not None:
        c.effort = body.effort
    await db.commit()
    return {"id": c.id, "title": c.title, "model": c.model, "effort": c.effort}


@router.delete("/{conv_id}")
async def delete_conversation(conv_id: str, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Conversation).where(Conversation.id == conv_id, Conversation.user_id == api_key.user_id))
    c = res.scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "not found")
    await db.delete(c)
    await db.commit()
    return {"deleted": True}


@router.get("/{conv_id}/export")
async def export_conversation(conv_id: str, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Conversation).where(Conversation.id == conv_id, Conversation.user_id == api_key.user_id))
    c = res.scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "not found")
    mres = await db.execute(select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at))
    msgs = mres.scalars().all()
    lines = [f"# {c.title or conv_id}", ""]
    for m in msgs:
        role = m.role.upper()
        lines.append(f"## {role}")
        if m.thinking:
            lines.append("### Thinking")
            lines.append(m.thinking)
        if m.tool_calls_json and m.tool_calls_json != "[]":
            lines.append("### Tool Calls")
            lines.append(m.tool_calls_json)
        lines.append(m.content or "")
        lines.append("")
    md = "\n".join(lines)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(md, media_type="text/markdown")


@router.get("/{conv_id}/artifacts")
async def list_artifacts(conv_id: str, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    from ...db.models import Artifact
    res = await db.execute(select(Artifact).where(Artifact.conversation_id == conv_id))
    # ownership check
    cres = await db.execute(select(Conversation).where(Conversation.id == conv_id, Conversation.user_id == api_key.user_id))
    if cres.scalar_one_or_none() is None:
        raise HTTPException(404, "not found")
    return [{"id": a.id, "filename": a.filename, "kind": a.kind, "size": a.size} for a in res.scalars().all()]


@router.post("/{conv_id}/fork")
async def fork_conversation(conv_id: str, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Conversation).where(Conversation.id == conv_id, Conversation.user_id == api_key.user_id))
    src = res.scalar_one_or_none()
    if src is None:
        raise HTTPException(404, "not found")
    newc = Conversation(user_id=src.user_id, claude_org_id=src.claude_org_id, claude_conv_id="", parent_id=src.id, title=f"Fork of {src.title or src.id[:8]}", model=src.model, effort=src.effort)
    db.add(newc)
    await db.flush()
    mres = await db.execute(select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at))
    for m in mres.scalars().all():
        db.add(Message(conversation_id=newc.id, parent_uuid=m.parent_uuid, role=m.role, content=m.content, thinking=m.thinking, tool_calls_json=m.tool_calls_json))
    db.add(Message(conversation_id=newc.id, role="system", content="This chat continues a previous conversation. Full prior context is included above. Respond accordingly and maintain continuity."))
    await db.commit()
    return {"id": newc.id, "parent_id": src.id}