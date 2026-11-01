"""Async client for claude.ai internal web API using curl_cffi."""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncGenerator

from curl_cffi.requests import AsyncSession

import re
BASE = "https://claude.ai"

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _valid_id(value: str) -> bool:
    return bool(value and _ID_RE.match(value))


class ClaudeWebClient:
    def __init__(self, cookies: dict, device_id: str = ""):
        self.cookies = cookies
        self.device_id = device_id
        self._session: AsyncSession | None = None

    async def __aenter__(self):
        self._session = AsyncSession(impersonate="chrome136")
        for k, v in self.cookies.items():
            self._session.cookies.set(k, v, domain=".claude.ai")
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()
            self._session = None

    def _headers(self) -> dict:
        h = {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": "https://claude.ai",
            "referer": "https://claude.ai/new",
            "anthropic-client-platform": "web_claude_ai",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        }
        if self.device_id:
            h["anthropic-device-id"] = self.device_id
        return h

    async def get_orgs(self) -> list[dict]:
        assert self._session
        r = await self._session.get(f"{BASE}/api/organizations", headers=self._headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("organizations") or data.get("data") or []
        return []

    async def get_org_id(self) -> str:
        orgs = await self.get_orgs()
        if orgs:
            return orgs[0].get("uuid") or orgs[0].get("id")
        # fallback
        r2 = await self._session.get(f"{BASE}/api/account", headers=self._headers(), timeout=30)
        r2.raise_for_status()
        acc = r2.json()
        memberships = acc.get("memberships") or []
        if memberships:
            return memberships[0].get("organization", {}).get("uuid")
        raise RuntimeError("Could not resolve organization id from claude.ai")

    async def get_org_details(self, org_id: str) -> dict:
        assert self._session
        r = await self._session.get(f"{BASE}/api/organizations/{org_id}", headers=self._headers(), timeout=30)
        if r.status_code == 200:
            return r.json()
        # fallback to list
        orgs = await self.get_orgs()
        for o in orgs:
            if (o.get("uuid") or o.get("id")) == org_id:
                return o
        return orgs[0] if orgs else {}

    async def get_bootstrap(self) -> dict:
        assert self._session
        r = await self._session.get(f"{BASE}/api/bootstrap", headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    async def list_models(self, org_id: str) -> list[dict]:
        data = await self.get_bootstrap()
        memberships = data.get("account", {}).get("memberships", []) if isinstance(data, dict) else []
        cfg = []
        for mem in memberships:
            org = mem.get("organization", {})
            if (org.get("uuid") or org.get("id")) == org_id:
                cfg = org.get("claude_ai_bootstrap_models_config") or []
                break
        if not cfg and memberships:
            cfg = memberships[0].get("organization", {}).get("claude_ai_bootstrap_models_config") or []
        out = []
        for m in cfg:
            mid = m.get("model") or m.get("id") or m.get("name")
            if not mid:
                continue
            out.append({
                "id": mid,
                "name": m.get("name") or mid,
                "active": not m.get("inactive", False),
                "overflow": m.get("overflow", False),
                "description": m.get("description", ""),
                "thinking_modes": m.get("thinking_modes", []),
                "hard_limit": m.get("hard_limit"),
            })
        if not out:
            from .models import MODELS
            out = [{"id": m["id"], "name": m["name"], "active": True, "overflow": False, "thinking_modes": []} for m in MODELS]
        return out

    async def get_usage(self, org_id: str) -> dict:
        assert self._session
        r = await self._session.get(f"{BASE}/api/organizations/{org_id}/usage", headers=self._headers(), timeout=30)
        if r.status_code == 200:
            return r.json()
        return {}

    async def create_conversation(self, org_id: str) -> str:
        assert self._session
        if not _valid_id(org_id):
            raise ValueError("invalid org_id")
        payload = {"uuid": str(uuid.uuid4()), "name": ""}
        r = await self._session.post(
            f"{BASE}/api/organizations/{org_id}/chat_conversations",
            json=payload,
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("uuid") or data.get("id")

    async def send(self, org_id: str, conv_id: str, payload: dict) -> AsyncGenerator[dict, None]:
        assert self._session
        if not _valid_id(org_id) or not _valid_id(conv_id):
            raise ValueError("invalid id")
        url = f"{BASE}/api/organizations/{org_id}/chat_conversations/{conv_id}/completion"
        headers = {**self._headers(), "accept": "text/event-stream"}
        r = await self._session.post(url, json=payload, headers=headers, stream=True, timeout=300)
        r.raise_for_status()
        event = None
        data_lines: list[str] = []
        async for raw in r.aiter_lines():
            line = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
            if line == "":
                if data_lines:
                    joined = "\n".join(data_lines)
                    try:
                        obj = json.loads(joined)
                    except json.JSONDecodeError:
                        obj = {"raw": joined}
                    yield {"event": event, "data": obj}
                    event = None
                    data_lines = []
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            joined = "\n".join(data_lines)
            try:
                obj = json.loads(joined)
            except json.JSONDecodeError:
                obj = {"raw": joined}
            yield {"event": event, "data": obj}

    async def delete_conversation(self, org_id: str, conv_id: str) -> None:
        assert self._session
        try:
            await self._session.delete(
                f"{BASE}/api/organizations/{org_id}/chat_conversations/{conv_id}",
                headers=self._headers(),
                timeout=30,
            )
        except Exception:
            pass