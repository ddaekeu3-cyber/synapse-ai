---
layout: solution
title: "OAuth re-login does not clear disabledUntil flag in usageStats"
category: auth
source: https://github.com/openclaw/openclaw/issues/49070
description: "When an OAuth profile's refresh token expires, OpenClaw correctly sets + in . This prevents the fallback chain from repeatedly hitting a dead"
---

# OAuth re-login does not clear disabledUntil flag in usageStats

## 증상
When an OAuth profile's refresh token expires, OpenClaw correctly sets `disabledUntil` + `disabledReason: refresh_token_expired_manual_disable` in `auth-profiles.json → usageStats`. This prevents the fallback chain from repeatedly hitting a dead profile.

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
Manually edit `auth-profiles.json` and delete `disabledUntil` + `disabledReason` from the affected profile's `usageStats` entry, then restart gateway.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49070
