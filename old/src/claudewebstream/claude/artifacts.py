"""Artifact detection and storage helpers."""

from __future__ import annotations

import hashlib
import os
import re
import uuid

from ..config import settings


def extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """Return list of (language, code) from fenced blocks."""
    pattern = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)
    out = []
    for m in pattern.finditer(text):
        lang = m.group(1) or "txt"
        out.append((lang, m.group(2)))
    return out


def save_artifact(conv_id: str, kind: str, filename: str, content: bytes) -> tuple[str, str]:
    settings.ensure_home()
    d = settings.storage_path / "artifacts" / conv_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / filename
    path.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    # index file
    idx = d / "index.json"
    import json
    entries = []
    if idx.exists():
        try:
            entries = json.loads(idx.read_text())
        except Exception:
            entries = []
    entries.append({"filename": filename, "path": str(path), "sha256": sha, "size": len(content), "kind": kind})
    idx.write_text(json.dumps(entries, indent=2))
    return str(path), str(idx)