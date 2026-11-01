"""SQLite persistence. WAL mode, foreign keys on, one connection serialised by aiosqlite.

Sensitive values (credentials, titles, messages, events, results, artifact bytes and metadata)
are stored as AES-GCM blobs produced by security.seal(). Identifiers, timestamps, statuses,
model names, token counts, event kinds, hashes and usernames are plaintext operational metadata.
"""
from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  kdf_salt BLOB NOT NULL,
  verifier BLOB NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS credentials (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  blob BLOB NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  secret_hash TEXT NOT NULL,
  scopes TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL,
  last_used_at REAL,
  revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title_blob BLOB NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  api_key_id TEXT,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  idempotency_key TEXT,
  error TEXT,
  result_blob BLOB,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idempotency
  ON runs(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  parent_id TEXT,
  run_id TEXT,
  role TEXT NOT NULL,
  blob BLOB NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS run_events (
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  blob BLOB NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  run_id TEXT,
  meta_blob BLOB NOT NULL,
  mime TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  path TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_user ON artifacts(user_id, created_at);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path, isolation_level=None)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=NORMAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.executescript(SCHEMA)

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()

    # ---- helpers -----------------------------------------------------------
    async def _run(self, sql: str, params: tuple = ()) -> int:
        cur = await self.db.execute(sql, params)
        try:
            return cur.rowcount
        finally:
            await cur.close()

    async def _one(self, sql: str, params: tuple = ()):
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, params: tuple = ()):
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchall()

    async def _insert(self, table: str, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        await self._run(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))

    async def _update(self, table: str, key_col: str, key, fields: dict) -> None:
        sets = ", ".join(f"{col} = ?" for col in fields)
        await self._run(f"UPDATE {table} SET {sets} WHERE {key_col} = ?", (*fields.values(), key))

    # ---- lifecycle ---------------------------------------------------------
    async def mark_interrupted(self) -> None:
        """Runs that were live when the process died cannot be resumed upstream; say so honestly."""
        now = time.time()
        await self._run(
            "UPDATE runs SET status='failed', error=?, updated_at=?, completed_at=? "
            "WHERE status IN ('created','queued','connecting','streaming')",
            ("gateway restarted while this run was active; the gateway cannot resume the upstream generation",
             now, now),
        )

    # ---- users and credentials ---------------------------------------------
    async def create_user(self, user_id: str, username: str, salt: bytes, verifier: bytes) -> None:
        await self._insert("users", {"id": user_id, "username": username, "kdf_salt": salt,
                                     "verifier": verifier, "created_at": time.time()})

    async def get_user_by_name(self, username: str):
        return await self._one("SELECT * FROM users WHERE username = ?", (username,))

    async def get_user(self, user_id: str):
        return await self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    async def list_users(self):
        return await self._all("SELECT id, username, created_at FROM users ORDER BY created_at")

    async def set_credential(self, user_id: str, blob: bytes) -> None:
        await self._run(
            "INSERT INTO credentials (user_id, blob, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at",
            (user_id, blob, time.time()),
        )

    async def get_credential(self, user_id: str) -> bytes | None:
        row = await self._one("SELECT blob FROM credentials WHERE user_id = ?", (user_id,))
        return row["blob"] if row else None

    async def delete_credential(self, user_id: str) -> None:
        await self._run("DELETE FROM credentials WHERE user_id = ?", (user_id,))

    # ---- gateway API keys --------------------------------------------------
    async def insert_api_key(self, row: dict) -> None:
        await self._insert("api_keys", row)

    async def get_api_key(self, key_id: str):
        return await self._one("SELECT * FROM api_keys WHERE id = ?", (key_id,))

    async def list_api_keys(self, user_id: str):
        return await self._all(
            "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC", (user_id,))

    async def revoke_api_key(self, user_id: str, key_id: str) -> int:
        return await self._run(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (time.time(), key_id, user_id))

    async def touch_api_key(self, key_id: str) -> None:
        now = time.time()
        await self._run(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)",
            (now, key_id, now - 60))

    # ---- conversations and messages ----------------------------------------
    async def insert_conversation(self, row: dict) -> None:
        await self._insert("conversations", row)

    async def touch_conversation(self, conversation_id: str, when: float) -> None:
        await self._run("UPDATE conversations SET updated_at = ? WHERE id = ?", (when, conversation_id))

    async def get_conversation(self, user_id: str, conversation_id: str):
        return await self._one(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id))

    async def list_conversations(self, user_id: str, limit: int = 200):
        return await self._all(
            "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit))

    async def insert_message(self, row: dict) -> None:
        await self._insert("messages", row)

    async def get_message(self, user_id: str, message_id: str):
        return await self._one(
            "SELECT * FROM messages WHERE id = ? AND user_id = ?", (message_id, user_id))

    async def list_messages(self, user_id: str, conversation_id: str):
        return await self._all(
            "SELECT * FROM messages WHERE user_id = ? AND conversation_id = ? ORDER BY created_at, rowid",
            (user_id, conversation_id))

    async def last_message_id(self, conversation_id: str) -> str | None:
        row = await self._one(
            "SELECT id FROM messages WHERE conversation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (conversation_id,))
        return row["id"] if row else None

    # ---- runs and events ---------------------------------------------------
    async def insert_run(self, row: dict) -> None:
        await self._insert("runs", row)

    async def update_run(self, run_id: str, fields: dict) -> None:
        await self._update("runs", "id", run_id, fields)

    async def get_run(self, user_id: str, run_id: str):
        return await self._one("SELECT * FROM runs WHERE id = ? AND user_id = ?", (run_id, user_id))

    async def get_run_by_idem(self, user_id: str, key: str):
        return await self._one(
            "SELECT * FROM runs WHERE user_id = ? AND idempotency_key = ?", (user_id, key))

    async def list_runs(self, user_id: str, limit: int = 50):
        return await self._all(
            "SELECT * FROM runs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))

    async def count_runs_by_status(self, user_id: str) -> dict:
        rows = await self._all(
            "SELECT status, COUNT(*) AS n FROM runs WHERE user_id = ? GROUP BY status", (user_id,))
        return {r["status"]: r["n"] for r in rows}

    async def usage_totals(self, user_id: str) -> dict:
        row = await self._one(
            "SELECT COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, COUNT(*) AS runs "
            "FROM runs WHERE user_id = ?", (user_id,))
        return {"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"], "runs": row["runs"]}

    async def insert_event(self, run_id: str, seq: int, kind: str, blob: bytes) -> None:
        await self._insert("run_events", {"run_id": run_id, "seq": seq, "kind": kind, "blob": blob})

    async def events_after(self, run_id: str, after: int, limit: int = 200):
        return await self._all(
            "SELECT seq, kind, blob FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (run_id, after, limit))

    # ---- artifacts ---------------------------------------------------------
    async def insert_artifact(self, row: dict) -> None:
        await self._insert("artifacts", row)

    async def list_artifacts(self, user_id: str, conversation_id: str | None = None):
        if conversation_id:
            return await self._all(
                "SELECT * FROM artifacts WHERE user_id = ? AND conversation_id = ? ORDER BY created_at DESC",
                (user_id, conversation_id))
        return await self._all(
            "SELECT * FROM artifacts WHERE user_id = ? ORDER BY created_at DESC", (user_id,))

    async def get_artifact(self, user_id: str, artifact_id: str):
        return await self._one(
            "SELECT * FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, user_id))
