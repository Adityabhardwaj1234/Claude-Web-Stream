from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.middleware import current_user_from_api_key, get_db
from ...config import settings
from ...db.models import Artifact, Conversation

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


async def _owned_artifact(db: AsyncSession, artifact_id: str, user_id: str) -> Artifact:
    res = await db.execute(select(Artifact).where(Artifact.id == artifact_id))
    a = res.scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "not found")
    cres = await db.execute(select(Conversation).where(Conversation.id == a.conversation_id, Conversation.user_id == user_id))
    if cres.scalar_one_or_none() is None:
        raise HTTPException(404, "not found")
    return a


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: str, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    a = await _owned_artifact(db, artifact_id, api_key.user_id)
    return {
        "id": a.id,
        "kind": a.kind,
        "filename": a.filename,
        "mime": a.mime,
        "size": a.size,
        "sha256": a.sha256,
        "conversation_id": a.conversation_id,
        "created_at": a.created_at,
    }


@router.get("/{artifact_id}/content")
async def get_artifact_content(artifact_id: str, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    a = await _owned_artifact(db, artifact_id, api_key.user_id)
    if not a.storage_path:
        raise HTTPException(404, "not found")
    root = os.path.abspath(settings.storage_path)
    path = os.path.abspath(a.storage_path)
    if not path.startswith(root) or not os.path.exists(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type=a.mime, filename=a.filename)