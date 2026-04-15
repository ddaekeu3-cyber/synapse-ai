---
layout: solution
title: "Discord slash sessions fail elevated allowFrom resolution; sandbox explain reports channel as unknown"
category: config
source: https://github.com/openclaw/openclaw/issues/53621
description: "Discord slash sessions can fail elevated permission resolution even when is configured"
---

# Discord slash sessions fail elevated allowFrom resolution; sandbox explain reports channel as unknown

## 증상
Discord slash sessions can fail elevated permission resolution even when `tools.elevated.allowFrom.discord` is configured correctly.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
-it hints point at `allowFrom` keys even though the configured values are already present.

So the bug has two user-visible effects:

- elevated slash flows do not work on Discord
- diagnostics suggest the wrong root cause

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53621
