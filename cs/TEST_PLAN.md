# Claude Stream test plan

Written for separate testing agents. Nothing here has been executed, and no scenario is marked as passed.

## Conventions

- Base URL `http://127.0.0.1:8787`. Use a fresh `CS_DATA_DIR` per run, for example `./testdata/run-N`.
- Use a fake upstream where possible. Point `CS_UPSTREAM_URL` at a local mock that speaks the Messages
  SSE format, so no real provider calls are made unless a scenario says so.
- "Session" means the cookie from `/api/auth/login` plus the `csrf` value from the same response, sent as
  `X-CSRF-Token` on writes. "Key" means a gateway key from `cs keys create` or the dashboard.
- Evidence means the HTTP status, the response body or headers, the relevant DB rows (use `sqlite3` read-only),
  and the server log lines. Never record a full key or password in evidence. Record the last four characters.

## Scenarios

| # | Scenario | Setup | Action | Expected | Evidence |
|---|---|---|---|---|---|
| 1 | Account creation and login | Empty data dir | Register `alice` with a 10+ char password. Log out. Log in. Try a wrong password. | 200 on register and login with `user_id`, `csrf`, `unlocked:true`. Wrong password returns 401 with the same message as an unknown user. | Responses; `users` row exists; no plaintext password in DB or log. |
| 2 | Password-derived encryption and decryption | Account with a stored key | Log out, then log in again and read `/api/credential`. | Hint `...abcd` matches the stored key's last four. | Credential hint; `credentials.blob` starts with `0x01`. |
| 3 | Wrong password and corrupted ciphertext | Account with a credential | Call `/api/auth/unlock` with a wrong password. Flip one byte in `credentials.blob` in a copy of the DB and read the credential. | Wrong password: 401 "incorrect password", vault stays locked. Corrupt blob: 500 `data_integrity`, no crash, other users unaffected. | Responses; server log has no stack trace containing secrets. |
| 4 | Provider credential storage and isolation | Users `alice` and `bob`, each with a different key | Read each user's `/api/credential`. Run `/v1/messages` with Alice's key and check the upstream request header. | Each user sees only their own hint. Upstream receives the correct key for the caller. | Mock upstream records `x-api-key`; DB shows distinct blobs. |
| 5 | Multiple API-key ownership | Alice with 3 keys | Create 3 keys, list them, and confirm each key id belongs to Alice. Try to revoke one with Bob's session. | Bob gets 404. Alice's list shows 3 keys with no secrets. | Responses; `api_keys` rows have Alice's `user_id`. |
| 6 | API-key revocation | Alice's key K1 working | Call `/api/models` with K1, revoke K1, call again. | First call 200, second call 401 "invalid API key". | Responses; `revoked_at` set. |
| 7 | Authentication and authorization | Keys with `read` only, and no key | Call `/v1/messages` with a `read`-only key. Call with no credentials, a malformed key, and an expired key. Call `POST /api/keys` with a gateway key. | 403 for missing scope; 401 for missing, malformed and expired; 403 "requires a dashboard session" for key management. | Status codes and bodies. |
| 8 | Model discovery and refresh | Mock `/v1/models` with pagination (`has_more`) | Call `/v1/models`, change the mock list, call again without refresh, then with `?refresh=1`. | Pages are merged. Second call returns cached data. Refresh returns the new list. Upstream 404 or 500 is surfaced as an error, not a crash. | Mock hit count; responses. |
| 9 | Model and effort validation | Mock upstream that returns 400 for an unsupported effort | Send `output_config.effort: "bogus"`, `max_tokens` missing, and a missing `model`. | Gateway-side: missing model or messages gives 400. Provider-side errors pass through as 400 with the upstream message. | Responses. |
| 10 | Normal text streaming | Mock emits message_start, two text deltas, message_delta, message_stop | `POST /v1/messages` with `stream:true`. | Client receives frames in order with `id:` increasing from 1. Concatenated text matches. `X-Run-Id` present. Run status completed. | Raw SSE capture; `runs` row; `run_events` count matches frames. |
| 11 | Thinking-event parsing | Mock emits thinking_delta and signature_delta blocks | Stream the request and feed the events to the Python SDK `Event.thinking`. | Thinking text is preserved, signature is persisted in the final message content. | SDK output; `GET /api/runs/{id}` message content. |
| 12 | Tool-use and web-search event handling | Mock emits tool_use with input_json_delta fragments, and server_tool_use for web search | Stream and then fetch the run. | Partial JSON is forwarded unchanged. Final stored input equals the parsed JSON. Unknown block types are kept, not dropped. | Captured events; final message content. |
| 13 | Long-duration streams | Mock that emits one token every 2 s for 10 min | Stream and watch for heartbeats. | Heartbeat comments every 15 s while idle. No disconnect. Run completes. | Timestamps of frames; memory RSS sampled every minute. |
| 14 | Slow downstream consumers | Mock with 5 MB of output | Read the stream at 1 KB/s. | Upstream is read at the consumer's pace only to the extent of DB writes. RSS does not grow unboundedly. Run completes. | RSS samples; timing. |
| 15 | Client disconnections | Long run | Disconnect the client after 3 events. Wait for completion. | Run continues to completion. Events are all persisted. No duplicate upstream request. | `runs.status`; event count; mock hit count equals 1. |
| 16 | SSE reconnection and event replay | Completed or live run | Connect with `Last-Event-ID: 5`, then with `?after=0`. | Replay begins at seq 6, or at seq 1, in order. A live run keeps delivering until terminal. | Captured frames; ids. |
| 17 | Duplicate request prevention | Same `Idempotency-Key` sent twice | Send two identical streaming requests with the same key, in parallel. | Only one upstream request is made. Both clients receive the same run id. | Mock hit count = 1; `X-Run-Id` equal. |
| 18 | Concurrent stream isolation | Two users, 3 streams each, with `CS_PER_USER_STREAMS=2` | Start 3 runs for Alice and 1 for Bob. | Alice's third run stays `queued` until one finishes. Bob's run is not delayed. Events never leak across runs. | Status timeline; events per run match the run's own text. |
| 19 | Database integrity | After scenarios 10 to 18 | Run `PRAGMA integrity_check` and `PRAGMA foreign_key_check`. Delete a user in a copy and confirm cascades. | Both checks report `ok` and no rows. Cascades remove keys, runs and events. | Check output. |
| 20 | Conversation branches | Conversation with 2 turns | Send a turn with `parent_message_id` pointing to the first assistant turn. | The new user message has that parent. Both branches are returned by `GET /api/conversations/{id}`. | Message `parent_id` values. |
| 21 | File uploads | A text file, a PNG, and a 26 MB file | Upload each with `POST /api/artifacts`. | Text and PNG stored with correct MIME. 26 MB returns 413. Digest in response matches `sha256sum`. | Responses; `artifacts` row; `sha256sum` of the download. |
| 22 | ZIP indexing and path traversal protection | ZIP with normal entries; ZIP with `../evil`; ZIP with an absolute path; a zip bomb-like archive | Upload each. | Normal ZIP indexed (`entries` count). Traversal and absolute paths rejected with 400. Oversized uncompressed total rejected. Nothing is written outside `artifacts/<user>/`. | Responses; filesystem listing under `CS_DATA_DIR`. |
| 23 | Artifact retrieval and authorization | Alice's artifact | Bob GETs Alice's artifact by id, then Alice downloads it. Tamper with the on-disk file and download again. | Bob gets 404. Alice gets the bytes. Tampered file yields 500 (decrypt failure) and no plaintext. | Responses. |
| 24 | Server restarts and interrupted runs | Run in `streaming` | Kill the process with SIGKILL. Restart `cs start`. Fetch the run. | Run is `failed` with the "restarted" note. Its events before the kill remain readable. Reattach returns the events and then ends. | `runs` row; events. |
| 25 | Memory consumption | 20 concurrent runs of 1,000 events each | Run the load and sample RSS. | RSS stays bounded. After completion RSS returns near baseline within a minute. No leaked tasks: `GET /health` `active_runs` returns to 0. | RSS samples; health output. |
| 26 | SDK compatibility | Python and TypeScript SDK | Run the SDK examples in `README.md` against the mock. | Text, thinking, tool events and errors all parse. `replay` resumes correctly. | Console output; exit codes. |
| 27 | Claude Code custom-base-URL integration | Claude Code installed, `ANTHROPIC_BASE_URL` set | Run a trivial prompt through the gateway. | Record exactly which requests Claude Code sends and which fail. Document gaps. Do not claim success unless a full round trip completes. | Claude Code version; request log; failures listed. |
| 28 | Windows startup and background operation | Windows 11, PowerShell | `cs start` in the foreground, then `Start-Process` in the background, then `cs status`, then stop the process. | Foreground prints the banner. Background starts hidden. Status reports running. Second start refuses with the "already running" message. | Console transcripts. |
| 29 | Security and rate-limiting behaviour | Account and mock | Send 200 requests per minute with a key. Send 11 wrong-password logins for one username. Send a cross-origin POST with `Origin: https://evil.example` and a session cookie but no CSRF header. Send a request with `Host: evil`. | Requests beyond the limit get 429 with `Retry-After`. Login limit returns 429 after 10. Cross-origin POST without CSRF returns 403. Security headers (CSP, X-Frame-Options, nosniff) are present on `/`. | Status codes; response headers. |

## Additional checks

- **Lock behaviour.** Lock the vault while a run is streaming. Expected: run is cancelled with `gateway.cancelled`,
  and the gateway key returns 423 until unlock.
- **Artifact content-type safety.** Upload an HTML file and open its download link. Expected: served as
  `attachment`, and the dashboard CSP stays in force.
- **Dashboard smoke test.** Register, store a key, create a gateway key, run a prompt in the workspace,
  press Reattach mid-run, then Cancel. Only the events the server actually emitted should appear.

## Exit criteria

All 29 scenarios and the additional checks have evidence attached. Each result is marked pass, fail, or blocked,
with the reason. Failures include a minimal reproduction.
