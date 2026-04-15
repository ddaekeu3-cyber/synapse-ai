---
layout: solution
title: "BlueBubbles webhook route returns 404 on gateway port (v2026.3.13)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48624
description: "Version: OpenClaw 2026.3.13"
---

# BlueBubbles webhook route returns 404 on gateway port (v2026.3.13)

## 증상
**Version:** OpenClaw 2026.3.13 (61d171a)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Running a local Node.js proxy on port 18793 that receives BlueBubbles webhooks and injects messages via `openclaw agent --channel bluebubbles --to <sender> --message <text> --deliver`. This works but loses typing indicators and is fragile.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48624
