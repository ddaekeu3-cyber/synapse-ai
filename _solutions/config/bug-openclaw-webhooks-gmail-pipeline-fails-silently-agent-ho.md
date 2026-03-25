---
layout: solution
title: "Bug: `openclaw webhooks gmail` pipeline fails silently — agent hook responses auto-suppressed + Tailscale App Store incompatibility"
category: config
source: https://github.com/openclaw/openclaw/issues/29090
---

# Bug: `openclaw webhooks gmail` pipeline fails silently — agent hook responses auto-suppressed + Tailscale App Store incompatibility

## 증상
- Tailscale (App Store version)

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Built a fully independent Python push handler that:
- Receives Pub/Sub pushes directly (port 8788)
- Fetches messages via `gog gmail history` + `gog gmail get`
- Sends to Telegram directly via Bot API (bypasses agent/announce entirely)
- Wakes main session via `/hooks/wake` for awareness
- Runs as launchd service, independent of the gateway
- Manages Tailscale Funnel via `--bg` flag (persists in daemon config)

Set `OPENCLAW_SKIP_GMAIL_WATCHER=1` in `env.vars` and `gateway.tailscale.mode: "off"` to prevent conflicts.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/29090
