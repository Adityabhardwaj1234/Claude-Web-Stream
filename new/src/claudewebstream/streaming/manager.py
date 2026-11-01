"""In-memory manager for active upstream stream connections."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from ..config import settings


@dataclass
class ActiveStream:
    stream_id: str
    conversation_id: str
    user_id: str
    task: asyncio.Task | None = None
    cancelled: bool = False
    chunks: list[dict] = field(default_factory=list)


class StreamManager:
    def __init__(self, max_concurrent: int | None = None):
        self.max_concurrent = max_concurrent or settings.max_concurrent_streams
        self._active: dict[str, ActiveStream] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, stream_id: str, conversation_id: str, user_id: str) -> ActiveStream | None:
        async with self._lock:
            if len(self._active) >= self.max_concurrent:
                return None
            st = ActiveStream(stream_id=stream_id, conversation_id=conversation_id, user_id=user_id)
            self._active[stream_id] = st
            return st

    async def release(self, stream_id: str) -> None:
        async with self._lock:
            self._active.pop(stream_id, None)

    def cancel(self, stream_id: str) -> bool:
        st = self._active.get(stream_id)
        if st is None:
            return False
        st.cancelled = True
        if st.task and not st.task.done():
            st.task.cancel()
        return True

    def active_count(self) -> int:
        return len(self._active)