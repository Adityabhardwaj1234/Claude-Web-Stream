"""Save/load claude.ai sessions encrypted with the user master key."""

from __future__ import annotations

import base64
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto.cipher import decrypt, encrypt
from ..db.models import ClaudeSession


async def save_session(session: AsyncSession, user_id: str, org_id: str, cookies: dict, device_id: str, master_key: bytes) -> ClaudeSession:
    from ..crypto.secrets import get_server_key
    payload = json.dumps({"cookies": cookies, "device_id": device_id}).encode()
    rec = ClaudeSession(
        user_id=user_id,
        org_id=org_id,
        cookies_enc=encrypt(payload, get_server_key()),
        cookies_user_enc=encrypt(payload, master_key),
        device_id=device_id,
    )
    session.add(rec)
    await session.flush()
    return rec


async def load_sessions(session: AsyncSession, user_id: str, master_key: bytes) -> list[tuple[ClaudeSession, dict, str]]:
    res = await session.execute(select(ClaudeSession).where(ClaudeSession.user_id == user_id))
    out = []
    for rec in res.scalars().all():
        try:
            data = json.loads(decrypt(rec.cookies_user_enc or rec.cookies_enc, master_key))
        except Exception:
            continue
        out.append((rec, data.get("cookies", {}), data.get("device_id", rec.device_id)))
    return out


async def load_runtime_sessions(session: AsyncSession, user_id: str) -> list[tuple[ClaudeSession, dict, str]]:
    from ..crypto.secrets import get_server_key
    res = await session.execute(select(ClaudeSession).where(ClaudeSession.user_id == user_id))
    out = []
    for rec in res.scalars().all():
        try:
            data = json.loads(decrypt(rec.cookies_enc, get_server_key()))
        except Exception:
            continue
        out.append((rec, data.get("cookies", {}), data.get("device_id", rec.device_id)))
    return out


async def delete_session(session: AsyncSession, session_id: str, user_id: str) -> bool:
    res = await session.execute(select(ClaudeSession).where(ClaudeSession.id == session_id, ClaudeSession.user_id == user_id))
    rec = res.scalar_one_or_none()
    if rec is None:
        return False
    await session.delete(rec)
    return True