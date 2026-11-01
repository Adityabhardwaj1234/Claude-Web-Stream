from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.api_keys import create_api_key, list_api_keys, revoke_api_key, reveal_api_key
from ...auth.middleware import current_user_from_api_key, get_db

router = APIRouter(prefix="/keys", tags=["keys"])


class CreateKeyIn(BaseModel):
    name: str = ""


@router.post("")
async def create_key(body: CreateKeyIn, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    rec, full = await create_api_key(db, api_key.user_id, body.name)
    await db.commit()
    return {"id": rec.id, "prefix": rec.prefix, "name": rec.name, "key": full}


@router.get("")
async def list_keys(api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    recs = await list_api_keys(db, api_key.user_id)
    return [{"id": r.id, "prefix": r.prefix, "name": r.name, "revoked": r.revoked, "created_at": r.created_at} for r in recs]


@router.post("/{key_id}/revoke")
async def revoke(key_id: str, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    ok = await revoke_api_key(db, key_id, api_key.user_id)
    await db.commit()
    if not ok:
        raise HTTPException(404, "not found")
    return {"revoked": True}


@router.get("/{key_id}/reveal")
async def reveal(key_id: str, api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    from ...db.models import ApiKey
    res = await db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == api_key.user_id))
    rec = res.scalar_one_or_none()
    if rec is None:
        raise HTTPException(404, "not found")
    return {"key": await reveal_api_key(rec)}