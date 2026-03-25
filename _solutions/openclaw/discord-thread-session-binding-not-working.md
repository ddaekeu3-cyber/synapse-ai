---
layout: solution
title: "Discord Thread Session Binding Not Working"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/38272
---

# Discord Thread Session Binding Not Working

## 증상
- OpenClaw Version: 2026.3.2 (85377a2)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Currently using `sessions_send` to manually forward messages to the session, but this defeats the purpose of thread binding.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/38272
