---
layout: solution
title: "OPENCLAW_GATEWAY_TOKEN in systemd service file not updated on config change or update, causing device_token_mismatch"
category: config
source: https://github.com/openclaw/openclaw/issues/17223
description: "After changing in , the systemd service file retains the old token hardcoded in . Since the env var overrides the config file value, the gateway process"
---

# OPENCLAW_GATEWAY_TOKEN in systemd service file not updated on config change or update, causing device_token_mismatch

## 증상
After changing `gateway.auth.token` in `openclaw.json`, the systemd service file retains the **old token** hardcoded in `Environment=OPENCLAW_GATEWAY_TOKEN=<old_token>`. Since the env var overrides the config file value, the gateway process uses a different token than what's in the config — causing `device_token_mismatch` for all internal tool calls (cron, sessions, etc.) and CLI connections.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Create a systemd override to match the current config token:

```bash
mkdir -p ~/.config/systemd/user/openclaw-gateway.service.d/
cat > ~/.config/systemd/user/openclaw-gateway.service.d/override.conf << 'EOF'
[Service]
Environment=OPENCLAW_GATEWAY_TOKEN=<your_current_config_token>
EOF
systemctl --user daemon-reload
systemctl --user restart openclaw-gateway
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/17223
