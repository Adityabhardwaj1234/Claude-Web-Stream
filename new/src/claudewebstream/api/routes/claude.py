from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.middleware import current_user_from_api_key, get_db
from ...claude.models import MODELS, EFFORTS
from ...services.browser_login import browser_login
from ...services.claude_sessions import save_session
from ...db.models import User
from ...claude.client import ClaudeWebClient

router = APIRouter(prefix="/claude", tags=["claude"])


@router.get("/models")
async def models():
    return {"data": MODELS, "efforts": EFFORTS}


@router.post("/link-session")
async def link_session(api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(User).where(User.id == api_key.user_id))
    u = res.scalar_one_or_none()
    if u is None:
        raise HTTPException(404, "user not found")
    import os
    master_key = os.urandom(32)
    cookies, _, device_id = await browser_login()
    async with ClaudeWebClient(cookies, device_id) as client:
        org_id = await client.get_org_id()
    await save_session(db, u.id, org_id, cookies, device_id, master_key)
    await db.commit()
    return {"org_id": org_id, "cookies": len(cookies)}
