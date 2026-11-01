# ClaudeWebStream

Local FastAPI server that streams claude.ai web chat responses as OpenAI-compatible SSE.

## Setup

```powershell
cd App
$env:PYTHONPATH='src'
python -m claudewebstream init
python -m claudewebstream register --username you --password secret
python -m claudewebstream claude-login --username you   # opens Chromium; log in, then it extracts cookies automatically
python -m claudewebstream key-create --username you --password secret
python -m claudewebstream start --port 8787
```

Short command: use `cws.cmd` or `cws.ps1`.

## Usage

```bash
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer cws_..." \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

## Docs
- Swagger: http://127.0.0.1:8787/docs
- Dashboard: http://127.0.0.1:8787/

## Limitations
- Uses unofficial claude.ai web API; cookie expiry requires re-login.
- Cloudflare may block; curl_cffi impersonation used.