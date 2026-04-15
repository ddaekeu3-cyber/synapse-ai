---
layout: solution
title: "Gateway crash: Uncaught TypeError in undici TLS setSession during sub-agent failover storm"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/35257
description: "Gateway crashes with an uncaught exception when multiple sub-agents trigger rapid provider failover, causing a TLS connection storm in"
---

# Gateway crash: Uncaught TypeError in undici TLS setSession during sub-agent failover storm

## 증상
Gateway crashes with an uncaught exception when multiple sub-agents trigger rapid provider failover, causing a TLS connection storm in undici.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Ensure all provider API keys referenced by sub-agent model aliases are present in `openclaw.json` env section to prevent failover storms.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/35257
