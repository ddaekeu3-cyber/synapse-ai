---
layout: solution
title: "Concurrent sub-agents trigger 'Not logged in' errors when ~/.claude/.credentials.json is absent (valid token, macOS Keychain)"
category: auth
source: https://github.com/anthropics/claude-code/issues/37893
---

# Concurrent sub-agents trigger "Not logged in" errors when ~/.claude/.credentials.json is absent (valid token, macOS Keychain)

## 증상
When multiple Claude Code sub-agents run concurrently on macOS, all agents intermittently receive "Not logged in" errors even though the OAuth token is valid and was issued less than an hour ago. The session recovers on its own within 1-2 minutes without any user action (re-login is not required).

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Creating a stub credentials file eliminates the `catch` branch:

```bash
echo '{}' > ~/.claude/.credentials.json
```

With the file present and mtime stable, `kA9()` becomes a no-op, `hA` retains its cached valid token, and the lock cascade cannot occur.

Verified this does **not** affect the 8-hour automatic token refresh: `vB(expiresAt)` returns `true` for both "about to expire" and "already expired" states, so `_P1()` still refreshes correctly when needed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37893
