"""Server-level secret key management."""

from __future__ import annotations

import base64
import os
from pathlib import Path

from ..config import settings


def _key_file() -> Path:
    settings.ensure_home()
    return settings.home / "server.key"


def get_server_key() -> bytes:
    """Return 32-byte server key, generating/persisting if needed."""
    if settings.secret_key:
        import hashlib
        return hashlib.sha256(settings.secret_key.encode()).digest()
    p = _key_file()
    if p.exists():
        return base64.b64decode(p.read_text().strip())
    key = os.urandom(32)
    p.write_text(base64.b64encode(key).decode())
    try:
        p.chmod(0o600)
    except Exception:
        pass
    return key