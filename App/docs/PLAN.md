# Plan

ClaudeWebStream turns a logged-in claude.ai browser session into a local, OpenAI-compatible streaming API.

## Goals
- Stream claude.ai responses (thinking, tool calls, web search, artifacts) as SSE.
- Multi-user accounts, multiple API keys per user, encrypted credentials.
- Conversation trees with forking and full context carry-over.
- Low memory: only active stream in memory.

## Phases
1. Core crypto/db/client/streaming (done)
2. Keys/ownership (done)
3. Persistence/replay (done)
4. Thinking/tools/efforts (done)
5. Artifacts (partial)
6. SDKs/website/docs (done)
7. Hardening (ongoing)