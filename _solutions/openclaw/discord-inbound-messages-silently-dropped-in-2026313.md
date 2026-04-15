---
layout: solution
title: "Discord inbound messages silently dropped in 2026.3.13"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49210
description: "- Upgraded from 2026.3.12"
---

# Discord inbound messages silently dropped in 2026.3.13

## 증상
- Upgraded from 2026.3.12 via `npm install -g openclaw@latest`

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Rolling back to 2026.3.12 (`npm install -g openclaw@2026.3.12`) immediately fixes the issue. Inbound messages process normally after rollback.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49210
