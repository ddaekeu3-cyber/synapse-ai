---
layout: solution
title: "voicecall CLI: EADDRINUSE when gateway voice-call plugin is already running"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47697
---

# voicecall CLI: EADDRINUSE when gateway voice-call plugin is already running

## 증상
`openclaw voicecall call` fails with `EADDRINUSE` when the voice-call plugin is already running inside the gateway, because both try to bind to the same webhook port.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None found. Cannot use CLI for outbound calls when the plugin is running in the gateway.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47697
