---
layout: solution
title: "Discord channel can show running=true while connected=false, and new DMs stop reaching the agent until gateway restart"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51190
---

# Discord channel can show running=true while connected=false, and new DMs stop reaching the agent until gateway restart

## 증상
Crash (process/app exits or hangs)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
openclaw gateway restart immediately restored Discord connectivity.
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51190
