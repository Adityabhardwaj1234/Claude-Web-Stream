"""Cryptography and credentials.

- Argon2id derives a per-user master key from the account password and a random salt.
- HKDF derives purpose-specific AES-256-GCM subkeys from the master key.
- Every sealed blob is versioned, and its purpose and record id are bound as associated data.
- Gateway API keys are random secrets; only their SHA-256 hash is stored.
- The Vault keeps unlocked master keys and dashboard sessions in process memory only.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT_V1 = b"\x01"
SESSION_TTL = 12 * 3600
VERIFIER_PLAINTEXT = b"claude-stream/verifier/v1"
_ARGON = dict(time_cost=3, memory_cost=64 * 1024, parallelism=4, hash_len=32, type=Type.ID)


class CorruptCiphertext(Exception):
    """Authenticated decryption failed: wrong key, tampered, truncated, or unknown format."""


def new_salt() -> bytes:
    return os.urandom(16)


def derive_master_key(password: str, salt: bytes) -> bytes:
    return hash_secret_raw(password.encode("utf-8"), salt, **_ARGON)


def _subkey(master: bytes, purpose: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=f"claude-stream/{purpose}/v1".encode(),
    ).derive(master)


def _bind(purpose: str, aad: bytes) -> bytes:
    return FORMAT_V1 + purpose.encode() + b"|" + aad


def seal(master: bytes, purpose: str, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    ct = AESGCM(_subkey(master, purpose)).encrypt(nonce, plaintext, _bind(purpose, aad))
    return FORMAT_V1 + nonce + ct


def open_(master: bytes, purpose: str, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < 1 + 12 + 16 or blob[:1] != FORMAT_V1:
        raise CorruptCiphertext("unknown or truncated ciphertext format")
    try:
        return AESGCM(_subkey(master, purpose)).decrypt(blob[1:13], blob[13:], _bind(purpose, aad))
    except InvalidTag as exc:
        raise CorruptCiphertext("authentication failed") from exc


def verifies(master: bytes, user_id: str, verifier_blob: bytes) -> bool:
    """A correct password decrypts the account verifier; a wrong one cannot."""
    try:
        return open_(master, "verifier", verifier_blob, user_id.encode()) == VERIFIER_PLAINTEXT
    except CorruptCiphertext:
        return False


# ---- gateway API keys -------------------------------------------------------

def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_gateway_key() -> tuple[str, str, str]:
    """Returns (key_id, full_key_shown_once, secret_hash_to_store)."""
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    return key_id, f"csk_{key_id}.{secret}", hash_secret(secret)


def parse_gateway_key(raw: str) -> tuple[str, str] | None:
    if not raw.startswith("csk_") or "." not in raw:
        return None
    key_id, _, secret = raw[4:].partition(".")
    return (key_id, secret) if key_id and secret else None


# ---- in-memory vault and sessions -------------------------------------------

@dataclass
class Session:
    user_id: str
    csrf: str
    created: float


class Vault:
    """Process-memory state only. Unlocked master keys expire after idle_seconds."""

    def __init__(self, idle_seconds: int = 1800):
        self.idle_seconds = idle_seconds
        self._keys: dict[str, list] = {}
        self._sessions: dict[str, Session] = {}

    # sessions (who is signed in to the dashboard)
    def new_session(self, user_id: str) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self._sessions[token] = Session(user_id, csrf, time.time())
        return token, csrf

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        sess = self._sessions.get(token)
        if sess and time.time() - sess.created > SESSION_TTL:
            self._sessions.pop(token, None)
            return None
        return sess

    def drop_session(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    # unlocked keys (whose encrypted data can be read or written right now)
    def unlock(self, user_id: str, master: bytes) -> None:
        self._keys[user_id] = [master, time.monotonic()]

    def master(self, user_id: str) -> bytes | None:
        entry = self._keys.get(user_id)
        if entry is None:
            return None
        if time.monotonic() - entry[1] > self.idle_seconds:
            self._keys.pop(user_id, None)
            return None
        entry[1] = time.monotonic()
        return entry[0]

    def lock(self, user_id: str) -> None:
        self._keys.pop(user_id, None)
