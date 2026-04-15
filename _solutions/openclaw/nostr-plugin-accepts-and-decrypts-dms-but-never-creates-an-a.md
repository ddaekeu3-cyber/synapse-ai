---
layout: solution
title: "Nostr plugin accepts and decrypts DMs but never creates an agent session"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48332
description: "Crash (process/app exits or"
---

# Nostr plugin accepts and decrypts DMs but never creates an agent session

## 증상
Crash (process/app exits or hangs)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manual decryption/reply via Node.js scripts works. The relay accepts writes and the bus decrypts reads. Only the gateway session dispatch is broken.
/volume1/docker/openclaw$
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48332
