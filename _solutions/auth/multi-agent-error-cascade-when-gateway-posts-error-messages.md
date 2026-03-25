---
layout: solution
title: "Multi-agent error cascade when gateway posts error messages to shared Slack channels"
category: auth
source: https://github.com/openclaw/openclaw/issues/51832
---

# Multi-agent error cascade when gateway posts error messages to shared Slack channels

## 증상
When multiple agents share a Slack channel with `allowBots: true`, gateway-posted error messages (e.g., OAuth failures) trigger a cascade loop. Each agent's error message triggers the other agent to attempt a response, which also fails and posts another error, creating a feedback loop of 1000+ messages in minutes.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Currently no good workaround. Options considered:
- `allowBots: false` - breaks inter-agent coordination
- `allowBots: "mentions"` - would work but prevents agents from seeing each other's non-mention messages
- Adding rules to AGENTS.md - agents never start (auth fails before prompt is loaded), so behavioral rules can't help

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51832
