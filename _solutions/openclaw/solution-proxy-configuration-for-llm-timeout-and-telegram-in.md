---
layout: solution
title: "Solution: Proxy configuration for LLM timeout and Telegram in restricted networks (China)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43629
description: "Encountered the following issues when using"
---

# Solution: Proxy configuration for LLM timeout and Telegram in restricted networks (China)

## 증상
Encountered the following issues when using OpenClaw:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
### Config File Location
`~/.openclaw/openclaw.json`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43629
