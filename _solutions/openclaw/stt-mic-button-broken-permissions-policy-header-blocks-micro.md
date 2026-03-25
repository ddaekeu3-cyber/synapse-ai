---
layout: solution
title: "STT mic button broken: Permissions-Policy header blocks microphone access"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51085
---

# STT mic button broken: Permissions-Policy header blocks microphone access

## 증상
The control-ui chat has a working STT implementation (`ui/src/ui/chat/speech.ts`) with a mic button wired into the chat view (`ui/src/ui/views/chat.ts`). However, the gateway's default security headers block microphone access, so clicking the mic button silently fails.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Change `microphone=()` to `microphone=(self)` so the dashboard's own origin can use the mic while still blocking third-party frames:

```ts
res.setHeader("Permissions-Policy", "camera=(), microphone=(self), geolocation=()");
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51085
