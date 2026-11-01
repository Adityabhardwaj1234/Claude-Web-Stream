"""User account management."""

from __future__ import annotations

import base64

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto.kdf import derive_key, new_salt
from ..crypto.passwords import hash_password, verify_password
from ..db.models import User


async def create_user(session: AsyncSession, username: str, password: str) -> User:
    salt = new_salt()
    user = User(username=username, password_hash=hash_password(password), kdf_salt=base64.b64encode(salt).decode())
    session.add(user)
    await session.flush()
    return user


async def get_user(session: AsyncSession, username: str) -> User | None:
    res = await session.execute(select(User).where(User.username == username))
    return res.scalar_one_or_none()


async def authenticate(session: AsyncSession, username: str, password: str) -> User | None:
    user = await get_user(session, username)
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def user_master_key(user: User, password: str) -> bytes:
    salt = base64.b64decode(user.kdf_salt)
    return derive_key(password, salt)