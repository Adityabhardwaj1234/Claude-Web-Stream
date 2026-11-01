from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..config import settings
from ..db.session import init_db
from ..streaming.manager import StreamManager
from .routes import auth, keys, claude, chat, streams, artifacts, usage, health, conversations

settings.ensure_home()


def create_app() -> FastAPI:
    app = FastAPI(title="ClaudeWebStream", version="0.1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:8787", "http://localhost:8787", "http://127.0.0.1:8000"], allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"])

    @app.on_event("startup")
    async def startup():
        await init_db()
        app.state.stream_manager = StreamManager()
        chat.set_manager(app.state.stream_manager)

    # routes
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(keys.router)
    app.include_router(claude.router)
    app.include_router(chat.router)
    app.include_router(streams.router)
    app.include_router(artifacts.router)
    app.include_router(usage.router)
    app.include_router(conversations.router)

    # static web
    web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "..", "web")
    web_dir = os.path.abspath(web_dir)
    if os.path.isdir(web_dir):
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app