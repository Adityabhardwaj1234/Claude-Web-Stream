"""Key derivation helpers (Argon2id)."""

from __future__ import annotations

import os
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id


def derive_key(password: str | bytes, salt: bytes, length: int = 32) -> bytes:
    """Derive a symmetric key from a password using Argon2id."""
    if isinstance(password, str):
        password = password.encode("utf-8")
    kdf = Argon2id(
        salt=salt,
        length=length,
        iterations=3,
        lanes=2,
        memory_cost=65536,
    )
    return kdf.derive(password)


def new_salt(length: int = 16) -> bytes:
    return os.urandom(length)