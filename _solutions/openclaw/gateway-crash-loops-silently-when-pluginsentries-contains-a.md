---
layout: solution
title: "Gateway crash-loops silently when plugins.entries contains a nonexistent plugin key"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/27455
---

# Gateway crash-loops silently when plugins.entries contains a nonexistent plugin key

## 증상
When `plugins.entries` in `openclaw.json` contains a key for a plugin that no longer exists in the installed version, the gateway crash-loops on every boot with no clear error pointing to the actual cause.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
```bash
jq 'del(.plugins.entries["google-antigravity-auth"])' ~/.openclaw/openclaw.json > /tmp/fix.json && mv /tmp/fix.json ~/.openclaw/openclaw.json
openclaw gateway restart
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27455
