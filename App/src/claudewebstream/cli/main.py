from __future__ import annotations

import asyncio
import base64
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

import typer

from ..config import settings
from ..db.session import init_db, SessionLocal
from ..auth.users import create_user, get_user, authenticate, user_master_key
from ..auth.api_keys import create_api_key, list_api_keys, revoke_api_key, reveal_api_key
from ..services.claude_sessions import save_session
from ..claude.client import ClaudeWebClient

app = typer.Typer(help="ClaudeWebStream CLI (cws)")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@app.command()
def init():
    settings.ensure_home()
    async def _():
        await init_db()
    _run(_())
    typer.echo(f"Initialized DB at {settings.db_path}")


@app.command()
def register(username: str = typer.Option(None, prompt=True), password: str = typer.Option(None, prompt=True, hide_input=True)):
    async def _():
        await init_db()
        async with SessionLocal() as db:
            if await get_user(db, username):
                typer.echo("username exists")
                return
            u = await create_user(db, username, password)
            await db.commit()
            typer.echo(f"created user {u.id}")
    _run(_())


@app.command()
def login(username: str = typer.Option(None, prompt=True), password: str = typer.Option(None, prompt=True, hide_input=True)):
    async def _():
        await init_db()
        async with SessionLocal() as db:
            u = await authenticate(db, username, password)
            if u is None:
                typer.echo("invalid credentials")
                return
            typer.echo(f"logged in {u.id}")
    _run(_())


@app.command("claude-login")
def claude_login(username: str = typer.Option(None, prompt=True), password: str = typer.Option(None, prompt=True, hide_input=True), browser: bool = typer.Option(True, help="Use Chromium tab automation")):
    async def _():
        await init_db()
        async with SessionLocal() as db:
            u = await get_user(db, username)
            if u is None:
                typer.echo("user not found; register first")
                return
            pwd = password
            if not pwd:
                pwd = getpass.getpass("password: ")
            from ..auth.users import authenticate as authfn
            uu = await authfn(db, username, pwd)
            if uu is None:
                typer.echo("invalid credentials")
                return
            master = user_master_key(uu, pwd)
            if browser:
                typer.echo("Launching browser. Log in to claude.ai in the opened window...")
                from ..services.browser_login import browser_login
                cookies, _org, device_id = await browser_login()
            else:
                cookie_input = typer.prompt("Paste claude.ai session cookie (or full cookie header)")
                cookies = {}
                for part in cookie_input.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookies[k.strip()] = v.strip()
                if "sessionKey" not in cookies:
                    cookies = {"sessionKey": cookie_input.strip()}
                device_id = cookies.get("anthropic-device-id", "")
            async with ClaudeWebClient(cookies, device_id) as client:
                org_id = await client.get_org_id()
                models = await client.list_models(org_id)
            await save_session(db, uu.id, org_id, cookies, device_id, master)
            await db.commit()
            typer.echo(f"linked org {org_id}")
            typer.echo(f"models: {[m['id'] for m in models]}")
    _run(_())


@app.command()
def start(host: str = "127.0.0.1", port: int = 8787, background: bool = False):
    settings.host = host
    settings.port = port
    if background:
        log = settings.home / "server.log"
        settings.ensure_home()
        import subprocess
        proc = subprocess.Popen([sys.executable, "-m", "claudewebstream.cli.main", "start", "--host", host, "--port", str(port)], stdout=open(log, "a"), stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        typer.echo(f"started pid {proc.pid}, log {log}")
        return
    import uvicorn
    from ..api.app import create_app
    uvicorn.run(create_app(), host=host, port=port)


@app.command("key-create")
def key_create(username: str = typer.Option(None, prompt=True), password: str = typer.Option(None, prompt=True, hide_input=True), name: str = ""):
    async def _():
        await init_db()
        async with SessionLocal() as db:
            from ..auth.users import authenticate as authfn
            u = await authfn(db, username, password)
            if u is None:
                typer.echo("invalid credentials")
                return
            rec, full = await create_api_key(db, u.id, name)
            await db.commit()
            typer.echo(f"key id {rec.id}")
            typer.echo(f"API KEY (save now): {full}")
    _run(_())


@app.command("key-list")
def key_list(username: str = typer.Option(None, prompt=True), password: str = typer.Option(None, prompt=True, hide_input=True)):
    async def _():
        await init_db()
        async with SessionLocal() as db:
            from ..auth.users import authenticate as authfn
            u = await authfn(db, username, password)
            if u is None:
                typer.echo("invalid credentials")
                return
            recs = await list_api_keys(db, u.id)
            for r in recs:
                typer.echo(f"{r.id} {r.prefix}... revoked={r.revoked} name={r.name}")
    _run(_())


@app.command("key-show")
def key_show(key_id: str, username: str = typer.Option(None, prompt=True), password: str = typer.Option(None, prompt=True, hide_input=True)):
    async def _():
        await init_db()
        async with SessionLocal() as db:
            from ..auth.users import authenticate as authfn
            u = await authfn(db, username, password)
            if u is None:
                typer.echo("invalid credentials")
                return
            recs = await list_api_keys(db, u.id)
            for r in recs:
                if r.id == key_id:
                    typer.echo(await reveal_api_key(r))
                    return
            typer.echo("not found")
    _run(_())


@app.command("key-revoke")
def key_revoke(key_id: str, username: str = typer.Option(None, prompt=True), password: str = typer.Option(None, prompt=True, hide_input=True)):
    async def _():
        await init_db()
        async with SessionLocal() as db:
            from ..auth.users import authenticate as authfn
            u = await authfn(db, username, password)
            if u is None:
                typer.echo("invalid credentials")
                return
            ok = await revoke_api_key(db, key_id, u.id)
            await db.commit()
            typer.echo("revoked" if ok else "not found")
    _run(_())


@app.command()
def usage(username: str = typer.Option(None, prompt=True), password: str = typer.Option(None, prompt=True, hide_input=True)):
    async def _():
        await init_db()
        async with SessionLocal() as db:
            from ..auth.users import authenticate as authfn
            u = await authfn(db, username, password)
            if u is None:
                typer.echo("invalid credentials")
                return
            from ..db.models import Usage
            from sqlalchemy import select
            res = await db.execute(select(Usage).where(Usage.user_id == u.id))
            for r in res.scalars().all():
                typer.echo(f"{r.day}: req={r.requests} in={r.prompt_tokens} out={r.completion_tokens}")
    _run(_())


@app.command()
def models(username: str = typer.Option(None, prompt=True), password: str = typer.Option(None, prompt=True, hide_input=True)):
    async def _():
        await init_db()
        async with SessionLocal() as db:
            from ..auth.users import authenticate as authfn
            u = await authfn(db, username, password)
            if u is None:
                typer.echo("invalid credentials")
                return
            from ..services.claude_sessions import load_runtime_sessions
            sessions = await load_runtime_sessions(db, u.id)
            if not sessions:
                typer.echo("no session")
                return
            rec, cookies, device_id = sessions[0]
            async with ClaudeWebClient(cookies, device_id) as client:
                ms = await client.list_models(rec.org_id)
            for m in ms:
                typer.echo(f"{m['id']} active={m.get('active')} overflow={m.get('overflow')}")
            # usage
            async with ClaudeWebClient(cookies, device_id) as client:
                us = await client.get_usage(rec.org_id)
            typer.echo(f"usage: {us}")
    _run(_())


if __name__ == "__main__":
    app()