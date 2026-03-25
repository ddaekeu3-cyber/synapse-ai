---
layout: solution
title: "Feature: Allow configurable cross-context messaging for trusted routes"
category: config
source: https://github.com/openclaw/openclaw/issues/22725
---

# Feature: Allow configurable cross-context messaging for trusted routes

## 증상
When an agent session is bound to one channel (e.g., iMessage), the `message` tool blocks sending to other channels (e.g., Discord) with:

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Spawn a sub-agent to do the cross-context send. Works but adds:
- Latency (~5-10s for sub-agent startup)
- Token cost (sub-agent overhead)
- Complexity (simple send becomes spawn+task)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/22725
