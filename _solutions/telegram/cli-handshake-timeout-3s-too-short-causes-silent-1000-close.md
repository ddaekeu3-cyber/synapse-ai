---
layout: solution
title: "CLI handshake timeout (3s) too short — causes silent 1000 close on loaded gateways"
category: telegram
source: https://github.com/openclaw/openclaw/issues/51274
description: "The CLI WebSocket handshake timeout is hardcoded to 3 seconds (DEFAULT_HANDSHAKE_TIMEOUT_MS = 3e3 in gateway-cli-*.js). On loaded gateways (600MB+ memory,"
---

# CLI handshake timeout (3s) too short — causes silent 1000 close on loaded gateways

## 증상
The CLI WebSocket handshake timeout is hardcoded to 3 seconds (DEFAULT_HANDSHAKE_TIMEOUT_MS = 3e3 in gateway-cli-*.js). On loaded gateways (600MB+ memory, 80+ tasks, multiple active channels), the gateway consistently fails to complete the handshake within 3s.

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
We patch the timeout to 15s via a systemd ExecStartPre drop-in that survives restarts and updates:

**~/bin/fix-handshake-timeout.sh:**
```bash
#!/bin/bash
DIST_DIR="$HOME/.npm-global/lib/node_modules/openclaw/dist"
for f in "$DIST_DIR"/gateway-cli-*.js; do
  [ -f "$f" ] || continue
  if grep -q "DEFAULT_HANDSHAKE_TIMEOUT_MS = 3e3" "$f"; then
    sed -i "s/DEFAULT_HANDSHAKE_TIMEOUT_MS = 3e3/DEFAULT_HANDSHAKE_TIMEOUT_MS = 15e3/g" "$f"
    echo "[fix-handshake] patched $f"
  fi
done
```

**~/.config/systemd/user/openclaw-gateway.service.d/fix-handshake.conf:**
```ini
[Service]
ExecStartPre=/bin/

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51274
