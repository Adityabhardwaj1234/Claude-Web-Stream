from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.users import authenticate, create_user, get_user
from ...db.session import SessionLocal

router = APIRouter(prefix="/auth", tags=["auth"])


class AuthIn(BaseModel):
    username: str
    password: str


async def _db():
    async with SessionLocal() as s:
        yield s


@router.post("/register")
async def register(body: AuthIn, db: AsyncSession = Depends(_db)):
    if await get_user(db, body.username):
        raise HTTPException(400, "username taken")
    u = await create_user(db, body.username, body.password)
    await db.commit()
    return {"id": u.id, "username": u.username}


@router.post("/login")
async def login(body: AuthIn, db: AsyncSession = Depends(_db)):
    u = await authenticate(db, body.username, body.password)
    if u is None:
        raise HTTPException(401, "invalid credentials")
    # ensure at least one API key exists for dashboard use
    from ...auth.api_keys import list_api_keys, create_api_key
    keys = await list_api_keys(db, u.id)
    active = [k for k in keys if not k.revoked]
    if active:
        from ...auth.api_keys import reveal_api_key
        key_value = await reveal_api_key(active[0])
    else:
        rec, key_value = await create_api_key(db, u.id, "dashboard")
        await db.commit()
    return {"id": u.id, "username": u.username, "api_key": key_value}