---
layout: solution
title: "/status slash command shows 'Done' without status card after 3.13→3.22 upgrade"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53189
---

# /status slash command shows 'Done' without status card after 3.13→3.22 upgrade

## 증상
After upgrading from OpenClaw v2026.3.13 to v2026.3.22, the `/status` slash command in Discord shows only **"✅ Done."** as an ephemeral response without rendering the status card (model, context usage, session info, etc.).

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
`session_status` tool from within a model session still works correctly and returns full status data. Only the Discord-facing `/status` slash command is affected.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53189
