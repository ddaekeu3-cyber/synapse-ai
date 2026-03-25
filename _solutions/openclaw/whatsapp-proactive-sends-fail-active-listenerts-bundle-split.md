---
layout: solution
title: "WhatsApp proactive sends fail — active-listener.ts bundle split creates duplicate Map instances"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52517
---

# WhatsApp proactive sends fail — active-listener.ts bundle split creates duplicate Map instances

## 증상
Proactive/outbound WhatsApp messages fail with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Downgrade to v2026.3.11:
```
npm install -g openclaw@2026.3.11
openclaw gateway restart
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52517
