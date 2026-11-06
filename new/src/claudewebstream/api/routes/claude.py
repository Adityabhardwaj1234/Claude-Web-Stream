from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.middleware import get_db, current_user_from_api_key
from ...claude.models import MODELS, EFFORTS
from ...services.claude_sessions import load_runtime_sessions
from ...claude.client import ClaudeWebClient

router = APIRouter(prefix="/claude", tags=["claude"])


@router.get("/models")
async def models(api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    sessions = await load_runtime_sessions(db, api_key.user_id)
    if sessions:
        rec, cookies, device_id = sessions[0]
        try:
            async with ClaudeWebClient(cookies, device_id) as client:
                ms = await client.list_models(rec.org_id)
            return {"data": ms, "efforts": EFFORTS}
        except Exception:
            pass
    return {"data": MODELS, "efforts": EFFORTS}


@router.post("/link-session")
async def link_session(api_key=Depends(current_user_from_api_key), db: AsyncSession = Depends(get_db)):
    from ...auth.users import get_user
    from ...db.models import User
    from sqlalchemy import select
    res = await db.execute(select(User).where(User.id == api_key.user_id))
    u = res.scalar_one_or_none()
    if u is None:
        raise HTTPException(404, "user not found")
    import os
    master_key = os.urandom(32)
    from ...services.browser_login import browser_login
    cookies, _, device_id = await browser_login()
    async with ClaudeWebClient(cookies, device_id) as client:
        org_id = await client.get_org_id()
    from ...services.claude_sessions import save_session
    await save_session(db, u.id, org_id, cookies, device_id, master_key)
    await db.commit()
    return {"org_id": org_id, "cookies": len(cookies)}
