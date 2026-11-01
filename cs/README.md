# Claude Stream (`cs`)

A local, encrypted, resumable streaming gateway for the Anthropic Messages API.

Claude Stream runs on your machine. You create an account in its dashboard, store your Anthropic
**API key** encrypted, and issue **gateway keys** to your applications and tools. Those applications
send Messages API requests to `http://127.0.0.1:8787/v1`, and the gateway forwards them upstream
with your stored key. Runs continue if a client disconnects, and events can be replayed with
`Last-Event-ID`.

It uses only the official Anthropic API. It does not use Claude.ai session cookies or private endpoints,
and a Claude.ai subscription is not an API subscription.

## Requirements

- Python 3.10 or newer
- An Anthropic API key (`sk-ant-...`) from the Anthropic Console

## Install

```bash
cd <repo-root>
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r app/requirements.txt
```

Make the `cs` command available:

```bash
# macOS / Linux
chmod +x app/cs
ln -s "$PWD/app/cs" ~/.local/bin/cs
```

```powershell
# Windows: add the app folder to your user PATH once, then open a new terminal
setx PATH "$env:PATH;C:\path\to\repo\app"
```

## Start

```bash
cs start          # foreground; Ctrl+C stops it
cs status         # is it running?
```

```
Claude Stream 1.0.0
  dashboard : http://127.0.0.1:8787/
  API base  : http://127.0.0.1:8787/v1
  data      : ~/.claude-stream
```

### Run in the background (Windows PowerShell)

```powershell
Start-Process -FilePath python -ArgumentList "-m","app.server","start" `
  -WorkingDirectory "C:\path\to\repo" -WindowStyle Hidden
cs status
```

Only one server should use a data directory. A PID-and-port lock file (`cs.lock`) refuses a second
start while the first is responding. If the process died and left the lock behind, the next `cs start`
detects that nothing answers and removes it.

## First use

1. Open **http://127.0.0.1:8787/** and create an account. Passwords are at least 10 characters.
2. Go to **Settings**, add your Anthropic API key, and save it. It is encrypted before it is written.
3. Go to **API keys**, create a key with the scopes you need, and copy it. It is shown once.

You can also create keys from the terminal. Key management reads the local database, so run it on the
machine that owns the data:

```bash
cs users
cs keys create --user alice --name laptop --scopes messages,read
cs keys list --user alice
cs keys revoke KEY_ID --user alice
```

## Connect an application

### Python

```python
# copy app/sdk/python_sdk.py into your project, or add app/sdk to PYTHONPATH
from python_sdk import ClaudeStream

with ClaudeStream("csk_YOUR_KEY", base_url="http://127.0.0.1:8787") as cs:
    for ev in cs.stream(model="MODEL_ID", max_tokens=512,
                        messages=[{"role": "user", "content": "Explain SSE in two sentences."}]):
        if ev.text:
            print(ev.text, end="", flush=True)
        if ev.tool_name:
            print(f"\n[tool started: {ev.tool_name}]")
        if ev.is_error:
            print("\n[error]", ev.data)
    print("\nrun:", cs.last_run_id)

    # Re-attach to a run after a dropped connection:
    # for ev in cs.replay(cs.last_run_id, after=last_seen_id): ...
```

Non-streaming: `cs.create(model=..., messages=...)` returns the final message object.

### TypeScript

```ts
import { ClaudeStream } from "./typescript_sdk";

const cs = new ClaudeStream({ apiKey: "csk_YOUR_KEY", baseUrl: "http://127.0.0.1:8787" });
for await (const ev of cs.stream({ model: "MODEL_ID", messages: [{ role: "user", content: "Hello" }] })) {
  if (ev.type === "content_block_delta" && ev.data.delta?.type === "text_delta") {
    process.stdout.write(ev.data.delta.text);
  }
}
```

### Raw HTTP

```bash
curl -N http://127.0.0.1:8787/v1/messages \
  -H "x-api-key: csk_YOUR_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"MODEL_ID","max_tokens":256,"stream":true,"messages":[{"role":"user","content":"Hello"}]}'
```

The response carries `X-Run-Id` and `X-Conversation-Id` headers. Each SSE frame has `id:`, `event:`
and `data:` lines. Pings are forwarded as keep-alives and are not stored. Heartbeat comments are sent
every 15 seconds while idle.

### Claude Code (experimental)

Point Claude Code at the gateway with a gateway key. Use the gateway key, not your Anthropic key:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_API_KEY=csk_YOUR_KEY
claude
```

Only the Messages API surface is implemented. Features that depend on other endpoints may not work
through the gateway. This has not been verified against a specific Claude Code release; treat it as
the first item on the compatibility checklist in `TEST_PLAN.md`.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/messages` | key `messages` or session | Messages API; `stream: true` for SSE. Optional `conversation_id`, `parent_message_id`. |
| GET | `/v1/models` | key `read` or session | Live model catalogue; `?refresh=1` bypasses the cache. |
| GET | `/api/runs` | `read` | Recent runs and their status and usage. |
| GET | `/api/runs/{id}` | `read` | One run, plus the final message when complete. |
| GET | `/api/runs/{id}/events` | `read` | Replay and follow SSE. Send `Last-Event-ID` or `?after=`. |
| POST | `/api/runs/{id}/cancel` | `messages` | Cancel a live run. |
| GET | `/api/conversations` | `read` | Conversations, decrypted titles. |
| GET | `/api/conversations/{id}` | `read` | Saved turns, decrypted. |
| POST | `/api/artifacts?conversation_id=` | `artifacts` | Upload raw bytes; `X-Filename` header. ZIPs are indexed. |
| GET | `/api/artifacts` | `artifacts` | Artifact metadata. |
| GET | `/api/artifacts/{id}/content` | `artifacts` | Decrypted file, with digest verification. |
| GET | `/api/overview` | `read` | Counts, token totals, limits. |
| GET | `/health` | none | Liveness, version, active runs. |

Dashboard-only routes (session cookie and `X-CSRF-Token` for writes): `/api/auth/*`, `/api/me`,
`/api/credential`, `/api/keys`.

Errors use the Anthropic shape: `{"type":"error","error":{"type":...,"message":...}}`.

## Streaming lifecycle and reconnection

A run moves through `created → queued → connecting → streaming → completed | failed | cancelled`.
Each transition is persisted. Every upstream event is written, encrypted, to `run_events` before
subscribers are told about it, so replay and live delivery read the same log.

- **Disconnects.** Closing the HTTP connection does not cancel the run.
- **Reconnects.** `GET /api/runs/{id}/events` with `Last-Event-ID: N` resumes after event N. The gateway
  never starts a second upstream generation for a reconnect.
- **Retries.** Upstream connection failures and 429, 5xx and 529 responses are retried (up to twice,
  with backoff) only if nothing has been sent to the client yet.
- **Idempotency.** An `Idempotency-Key` header returns the existing run for that key. It does not create
  a second generation.
- **Restarts.** Runs that were live when the process stopped are marked `failed` with an explicit note.
  The gateway cannot resume an upstream generation, and it does not claim to.
- **Limits.** `CS_MAX_STREAMS` bounds concurrent upstream generations overall, and `CS_PER_USER_STREAMS`
  bounds them per account. Excess runs wait in `queued`.

## Conversations and context

The gateway records each run in a conversation. It never injects history silently. What a request
contains is exactly what the model sees. The dashboard's "Continue this chat" sends the full local
history, and SDK callers should do the same. `conversation_id` attaches the new turn to an existing
conversation for storage. `parent_message_id` branches from an earlier turn.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CS_DATA_DIR` | `~/.claude-stream` | Database, encrypted artifacts, lock file |
| `CS_PORT` | `8787` | Port for `cs start` and `cs status` |
| `CS_HOST` | `127.0.0.1` | Bind address (non-loopback requires `--allow-remote`) |
| `CS_UPSTREAM_URL` | `https://api.anthropic.com` | Provider base URL |
| `CS_MAX_STREAMS` | `8` | Concurrent upstream generations |
| `CS_PER_USER_STREAMS` | `3` | Concurrent upstream generations per account |
| `CS_UPSTREAM_READ_TIMEOUT` | `600` | Seconds allowed between upstream bytes |
| `CS_RATE_PER_MINUTE` | `120` | Requests per minute per account |

## Security and the encryption boundary

- **Account keys.** Argon2id (time 3, 64 MiB, parallelism 4) derives a master key from the password and a
  random 16-byte salt. HKDF derives separate AES-256-GCM subkeys for credentials, events, messages,
  results, titles and artifacts. Each ciphertext carries a version byte, a random 96-bit nonce, and
  purpose and record-id associated data, so blobs cannot be moved between records undetected.
- **Password checks.** A verifier blob is encrypted at registration. A wrong password fails to decrypt it,
  and the login is rejected without touching data.
- **Encrypted at rest.** Anthropic key, message content, titles, run events, final results, artifact
  bytes and artifact filenames and indexes.
- **Plaintext metadata.** Ids, timestamps, statuses, model names, effort, token counts, event kinds,
  artifact MIME types, sizes and SHA-256 digests, usernames, and the hashes of gateway keys. The SQLite
  file is not encrypted as a whole. Only the records listed above are.
- **Gateway keys.** `csk_<id>.<secret>`. The secret is 256 random bits. Only its SHA-256 is stored.
  Keys can be scoped, expired, rotated and revoked.
- **Vault.** Unlocked master keys live in process memory only. They expire after 30 minutes idle.
  Locking or logging out cancels that account's live runs, because they could no longer persist events.
  After a restart, unlock in the dashboard before gateway keys can reach the provider. This is by design.
- **Sessions.** An HttpOnly, SameSite=Strict cookie with a 12-hour lifetime. Dashboard writes require a
  per-session CSRF token. Gateway keys do not use cookies, so CSRF does not apply to them.
- **Network.** Binds to `127.0.0.1` by default. No CORS middleware is enabled, so other origins cannot call
  the API from a browser. The dashboard sends a restrictive Content-Security-Policy and frame denial.
- **Logs.** Credentials, prompts and keys are not logged. Errors are logged with a generic message.

## Layout

```
app/
  __init__.py      version
  server.py        FastAPI app, routes, CLI (cs start/status/users/keys)
  engine.py        upstream adapter, run lifecycle, replay and fan-out
  storage.py       SQLite schema and repositories
  security.py      Argon2id, HKDF, AES-GCM, gateway keys, vault and sessions
  web/index.html   dashboard (single file)
  sdk/python_sdk.py
  sdk/typescript_sdk.ts
  requirements.txt, README.md, TEST_PLAN.md, LICENSE, cs, cs.cmd
```

## Limitations

- **Tools.** The gateway forwards client-side tool calls and server tools untouched. It never executes tools.
- **Artifacts.** Uploads are supported and ZIPs are indexed, but there is no server-side tool that creates
  artifacts from model output yet.
- **Capabilities.** Effort and thinking fields are passed through. The gateway does not know each model's
  supported settings, so the provider's validation errors are returned as-is.
- **Message memory.** The final assistant message is kept in memory until the run completes, because
  conversation storage needs the complete message. Streaming output itself is not buffered.
- **Single instance.** The lock is advisory and based on a live health check. It is not an OS-level file lock.
- **Usernames** are stored in plaintext to allow login lookup.
- **Claude Code** compatibility is unverified (see above).
- **Untested.** The code has not been executed or tested. The testing agents should start with `TEST_PLAN.md`.
