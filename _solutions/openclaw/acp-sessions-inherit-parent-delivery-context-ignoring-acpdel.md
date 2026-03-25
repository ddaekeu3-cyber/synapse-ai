---
layout: solution
title: "ACP sessions inherit parent delivery context, ignoring acp.delivery.mode"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/33859
---

# ACP sessions inherit parent delivery context, ignoring acp.delivery.mode

## 증상
When using `sessions_spawn` with `runtime: "acp"`, the ACP session output is delivered to the parent session's channel (Telegram in this case) regardless of `acp.delivery.mode` setting.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Run acpx directly via `exec` instead of using `sessions_spawn`:
```bash
/usr/lib/node_modules/openclaw/extensions/acpx/node_modules/.bin/acpx \
  --cwd /workspace \
  --approve-all claude exec "prompt"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33859
