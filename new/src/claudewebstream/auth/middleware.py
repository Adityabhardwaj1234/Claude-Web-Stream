"""FastAPI auth dependencies for users and API keys."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import SessionLocal
from .api_keys import verify_api_key
from .users import get_user


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def current_user_from_api_key(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    key = authorization.split(" ", 1)[1].strip()
    rec = await verify_api_key(db, key)
    if rec is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return rec  # ApiKey row; .user_id available