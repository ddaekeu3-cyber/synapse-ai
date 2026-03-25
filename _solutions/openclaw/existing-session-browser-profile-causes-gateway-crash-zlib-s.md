---
layout: solution
title: "existing-session browser profile causes gateway crash (zlib segfault during MCP handshake)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47965
---

# existing-session browser profile causes gateway crash (zlib segfault during MCP handshake)

## 증상
Crash (process/app exits or hangs)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Remove the `user` profile from config to restore normal browser service operation:
```json
// Remove this from browser.profiles:
"user": { "driver": "existing-session", "attachOnly": true, "color": "#00AA00" }
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47965
