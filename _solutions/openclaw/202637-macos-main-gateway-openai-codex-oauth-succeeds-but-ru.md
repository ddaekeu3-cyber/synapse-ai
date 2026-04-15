---
layout: solution
title: "2026.3.7 macOS MAIN gateway: OpenAI Codex OAuth succeeds but runtime fails (api: undefined / Not Found), MiniMax works"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/39917
description: "Regression (worked before, now"
---

# 2026.3.7 macOS MAIN gateway: OpenAI Codex OAuth succeeds but runtime fails (api: undefined / Not Found), MiniMax works

## 증상
Regression (worked before, now fails)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Set default model to MiniMax and restart:
- `openclaw --profile default models set minimax`
- `openclaw --profile default gateway restart`

This restores normal agent replies immediately.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39917
