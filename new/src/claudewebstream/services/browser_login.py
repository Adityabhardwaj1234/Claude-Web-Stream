"""Launch a browser and extract claude.ai session via CDP."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser() -> str:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    raise FileNotFoundError("No Chromium-based browser found")


async def _cdp_call(ws, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        data = json.loads(raw)
        if data.get("id") == msg_id:
            return data


async def browser_login(timeout: int = 300) -> tuple[dict, str, str]:
    browser = find_browser()
    print(f"Using browser: {browser}", flush=True)
    port = 9222
    profile = tempfile.mkdtemp(prefix="cws-chrome-")
    proc = subprocess.Popen(
        [browser, f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "https://claude.ai"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        print("Waiting for browser debugger...", flush=True)
        for _ in range(30):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
                break
            except Exception:
                await asyncio.sleep(1)
        import websockets
        # find a page target
        ws_url = None
        for _ in range(30):
            try:
                targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list").read())
                for t in targets:
                    if t.get("type") == "page":
                        ws_url = t.get("webSocketDebuggerUrl")
                        break
                if ws_url:
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
        if not ws_url:
            # fallback to browser-level
            ws_url = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version").read())["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url) as ws:
            await _cdp_call(ws, "Network.enable", {}, 1)
            await _cdp_call(ws, "Page.enable", {}, 2)
            await _cdp_call(ws, "Page.navigate", {"url": "https://claude.ai"}, 3)
            cookies = {}
            device_id = ""
            print("Browser opened. Please log in to claude.ai. Waiting for session cookie...", flush=True)
            for i in range(timeout // 2):
                await asyncio.sleep(2)
                try:
                    c_res = await _cdp_call(ws, "Network.getAllCookies", {}, 11)
                except Exception as e:
                    print("cdp error:", e, flush=True)
                    continue
                cookie_list = c_res.get("result", {}).get("cookies", [])
                cookies = {c["name"]: c["value"] for c in cookie_list if "claude" in (c.get("domain") or "")}
                if i % 5 == 0:
                    print(f"... waiting ({i*2}s), cookies seen: {list(cookies.keys())}", flush=True)
                if "sessionKey" in cookies:
                    device_id = cookies.get("anthropic-device-id", "")
                    print("Session cookie captured. Finishing...", flush=True)
                    break
            if "sessionKey" not in cookies:
                raise RuntimeError("Login not completed: sessionKey cookie not found")
            return cookies, "", device_id
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()