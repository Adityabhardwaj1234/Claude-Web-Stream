# Architecture

## Components
- FastAPI app factory (`api/app.py`)
- SQLite via SQLAlchemy async (`db/`)
- curl_cffi client (`claude/client.py`) for TLS-impersonated claude.ai calls
- StreamManager (`streaming/manager.py`) caps concurrent upstream connections
- Services for sessions, conversations, artifacts
- CLI (`cli/main.py`) with `cws` entrypoint

## Data flow
Client -> FastAPI auth middleware (API key) -> load runtime claude session -> create/reuse conversation -> ClaudeWebClient.send -> SSE events -> OpenAI chunks + persistence.

## Security
- User master key via Argon2id from password.
- Server secret key encrypts API keys, stream chunks, runtime cookies.
- User-factor copy stored for audit/compliance.