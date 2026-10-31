from __future__ import annotations

from fastapi import APIRouter

from ...claude.models import MODELS, EFFORTS

router = APIRouter(prefix="/claude", tags=["claude"])


@router.get("/models")
async def models():
    return {"data": MODELS, "efforts": EFFORTS}