---
layout: solution
title: "macOS TCC permissions break when node binary path changes during update"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/22179
description: "When OpenClaw auto-updates via (or manual ), if the underlying Node.js binary path changes (e.g. from to , or a Homebrew upgrade), all macOS TCC"
---

# macOS TCC permissions break when node binary path changes during update

## 증상
When OpenClaw auto-updates via `update-openclaw` (or manual `npm install -g openclaw@latest`), if the underlying Node.js binary path changes (e.g. from `node@24` to `node@25`, or a Homebrew upgrade), **all macOS TCC (Transparency, Consent, and Control) permissions are silently lost**. This breaks:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manually copy TCC permissions from old node binary to new one:

```sql
sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db "
INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version, indirect_object_identifier, indirect_object_identifier_type, flags)
SELECT service, '/opt/homebrew/Cellar/node/25.5.0/bin/node', client_type, auth_value, auth_reason, auth_version, indirect_object_identifier, indirect_object_identifier_type, flags
FROM access
WHERE client='/opt/homebrew/Cellar/node@24/24.13.0/bin/node' AND auth_value=2;"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/22179
