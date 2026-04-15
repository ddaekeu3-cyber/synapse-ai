---
layout: solution
title: "HTTP 401/403 misclassified as rate_limit with exponential cooldown — no self-recovery path"
category: auth
source: https://github.com/openclaw/openclaw/issues/47720
description: "- Ubuntu 22.04 VM (r430a), Node"
---

# HTTP 401/403 misclassified as rate_limit with exponential cooldown — no self-recovery path

## 증상
- Ubuntu 22.04 VM (r430a), Node 22.22.0

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
Manually edit `~/.openclaw/agents/main/agent/auth-profiles.json`:
- Set `errorCount` to `0`
- Remove `cooldownUntil` and `disabledUntil` fields
- Clear `failureCounts`
- Restart gateway

Or have another OpenClaw instance re-insert the API key, which creates a fresh auth profile hash.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47720
