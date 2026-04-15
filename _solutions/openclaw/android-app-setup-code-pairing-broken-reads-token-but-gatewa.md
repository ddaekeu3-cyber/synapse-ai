---
layout: solution
title: "Android app: setup code pairing broken — reads 'token' but gateway emits 'bootstrapToken'"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48926
description: "Android app fails to pair via setup code. The gateway (v2026.3.9+) emits in the setup code JSON payload, but reads , so it always gets and connects"
---

# Android app: setup code pairing broken — reads "token" but gateway emits "bootstrapToken"

## 증상
Android app fails to pair via setup code. The gateway (v2026.3.9+) emits `bootstrapToken` in the setup code JSON payload, but `GatewayConfigResolver.kt` reads `"token"`, so it always gets `null` and connects without a token → `reason=token_missing` on every attempt.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
```kotlin
val token = jsonField(obj, "bootstrapToken") ?: jsonField(obj, "token")
```

The fallback to `"token"` preserves compatibility with any older setup codes that used the original key.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48926
