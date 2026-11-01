from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.middleware import current_user_from_api_key, get_db
from ...db.models import Usage

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("")
async def usage(api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Usage).where(Usage.user_id == api_key.user_id))
    rows = res.scalars().all()
    return [{"day": r.day, "requests": r.requests, "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens} for r in rows]