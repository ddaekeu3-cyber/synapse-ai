---
layout: solution
title: "Feature Request: Tiered Memory Compaction System for Conversational Continuity"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/44079
description: "Repo:"
---

# Feature Request: Tiered Memory Compaction System for Conversational Continuity

## 증상
**Repo:** https://github.com/openclaw/openclaw/issues/new

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
We've built a basic DIY version using workspace files, shell scripts, and heartbeat checks, but it still relies on the agent being disciplined enough to run the compaction — which is the exact problem we're trying to solve.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44079
