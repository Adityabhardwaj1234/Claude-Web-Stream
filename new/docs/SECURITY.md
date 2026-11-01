# Security

- Trust boundary: local machine only by default (127.0.0.1).
- Passwords hashed with Argon2.
- User master key derived via Argon2id (64MB, 3 iters).
- API keys stored as SHA-256 hash + AESGCM(server key).
- Claude cookies stored AESGCM(server key) for runtime and AESGCM(user key) for user-factor.
- No secrets in logs.
- File downloads validated by path existence.