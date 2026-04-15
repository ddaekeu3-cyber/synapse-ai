---
layout: solution
title: "v2026.3.12: ReferenceError: Cannot access 'ANTHROPIC_MODEL_ALIASES' before initialization — breaks config loading and BlueBubbles webhook registration"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45124
description: "After upgrading to OpenClaw 2026.3.12 (6472949), every config load triggers a in . This causes the BlueBubbles webhook route () to silently fail to"
---

# v2026.3.12: ReferenceError: Cannot access 'ANTHROPIC_MODEL_ALIASES' before initialization — breaks config loading and BlueBubbles webhook registration

## 증상
After upgrading to OpenClaw **2026.3.12 (6472949)**, every config load triggers a `ReferenceError` in `normalizeAnthropicModelId()`. This causes the BlueBubbles webhook route (`/bluebubbles-webhook`) to silently fail to register, resulting in the BlueBubbles channel becoming completely unresponsive (no incoming messages are processed).

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
A full gateway restart (`openclaw gateway stop` followed by `openclaw gateway`) resolves the issue temporarily. However, the `ANTHROPIC_MODEL_ALIASES` error continues to appear on every config reload, and the webhook may break again after a config hot-reload or long uptime.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45124
