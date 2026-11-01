"""API key generation, storage, and verification."""

from __future__ import annotations

import base64
import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto.cipher import decrypt, encrypt
from ..crypto.secrets import get_server_key
from ..db.models import ApiKey


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> str:
    return "cws_" + secrets.token_urlsafe(32)


async def create_api_key(session: AsyncSession, user_id: str, name: str = "") -> tuple[ApiKey, str]:
    full = generate_key()
    rec = ApiKey(
        user_id=user_id,
        prefix=full[:12],
        key_hash=_hash_key(full),
        key_enc=encrypt(full.encode(), get_server_key()),
        name=name,
    )
    session.add(rec)
    await session.flush()
    return rec, full


async def verify_api_key(session: AsyncSession, key: str) -> ApiKey | None:
    h = _hash_key(key)
    res = await session.execute(select(ApiKey).where(ApiKey.key_hash == h))
    rec = res.scalar_one_or_none()
    if rec is None or rec.revoked:
        return None
    return rec


async def list_api_keys(session: AsyncSession, user_id: str) -> list[ApiKey]:
    res = await session.execute(select(ApiKey).where(ApiKey.user_id == user_id))
    return list(res.scalars().all())


async def revoke_api_key(session: AsyncSession, key_id: str, user_id: str) -> bool:
    res = await session.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id))
    rec = res.scalar_one_or_none()
    if rec is None:
        return False
    from datetime import datetime, timezone
    rec.revoked_at = datetime.now(timezone.utc)
    return True


async def reveal_api_key(rec: ApiKey) -> str:
    return decrypt(rec.key_enc, get_server_key()).decode()