from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.middleware import current_user_from_api_key, get_db
from ...crypto.cipher import decrypt
from ...crypto.secrets import get_server_key
from ...db.models import Conversation, Stream, StreamChunk

router = APIRouter(prefix="/streams", tags=["streams"])


async def _owned_stream(db: AsyncSession, stream_id: str, user_id: str) -> Stream:
    res = await db.execute(select(Stream).where(Stream.id == stream_id))
    s = res.scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "not found")
    cres = await db.execute(select(Conversation).where(Conversation.id == s.conversation_id, Conversation.user_id == user_id))
    if cres.scalar_one_or_none() is None:
        raise HTTPException(404, "not found")
    return s


@router.get("/{stream_id}")
async def get_stream(stream_id: str, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    s = await _owned_stream(db, stream_id, api_key.user_id)
    cres = await db.execute(select(StreamChunk).where(StreamChunk.stream_id == stream_id).order_by(StreamChunk.seq))
    chunks = []
    for c in cres.scalars().all():
        try:
            payload = decrypt(c.payload_enc, get_server_key()).decode()
        except Exception:
            payload = ""
        chunks.append({"seq": c.seq, "kind": c.kind, "payload": payload})
    return {"id": s.id, "status": s.status, "chunk_count": s.chunk_count, "chunks": chunks}