"""AES-GCM authenticated encryption helpers."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def encrypt(plaintext: bytes, key: bytes) -> str:
    """Return base64(nonce || ciphertext)."""
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(token: str, key: bytes) -> bytes:
    raw = base64.b64decode(token.encode("ascii"))
    nonce, ct = raw[:12], raw[12:]
    aes = AESGCM(key)
    return aes.decrypt(nonce, ct, None)