"""Claude model registry and effort levels."""

from __future__ import annotations

MODELS = [
    {"id": "claude-opus-4", "name": "Claude Opus 4"},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
    {"id": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5"},
    {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5"},
]

EFFORTS = ["low", "medium", "high", "max"]

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_EFFORT = "medium"


def normalize_model(m: str | None, available: list[dict] | None = None) -> str:
    pool = available or [{"id": x["id"]} for x in MODELS]
    ids = [x.get("id") for x in pool if x.get("id")]
    if m and m in ids:
        return m
    if m:
        low = m.lower()
        for mid in ids:
            if low in mid.lower():
                return mid
    # first active model if available
    for x in pool:
        if x.get("active", True) and x.get("id"):
            return x["id"]
    return ids[0] if ids else DEFAULT_MODEL


def normalize_effort(e: str | None, available_efforts: list[str] | None = None) -> str:
    pool = available_efforts or EFFORTS
    if e in pool:
        return e  # type: ignore
    return pool[0] if pool else DEFAULT_EFFORT


def efforts_from_models(models: list[dict]) -> list[str]:
    out = []
    for m in models:
        for key in ("efforts", "effort_levels", "supported_efforts"):
            val = m.get(key)
            if isinstance(val, list):
                for v in val:
                    if isinstance(v, str) and v not in out:
                        out.append(v)
        # thinking modes from claude.ai bootstrap
        for tm in m.get("thinking_modes", []) or []:
            if isinstance(tm, dict):
                tid = tm.get("id")
                if isinstance(tid, str) and tid not in out:
                    out.append(tid)
        cap = m.get("capabilities") or {}
        eff = cap.get("effort") if isinstance(cap, dict) else None
        if isinstance(eff, dict):
            for k, v in eff.items():
                if v and k not in out:
                    out.append(k)
    return out if out else list(EFFORTS)