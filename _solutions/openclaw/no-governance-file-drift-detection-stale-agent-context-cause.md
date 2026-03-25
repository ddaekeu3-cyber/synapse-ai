---
layout: solution
title: "No governance file drift detection - stale agent context causes silent coordination failures"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/35805
---

# No governance file drift detection - stale agent context causes silent coordination failures

## 증상
**Supersedes:** #7050 (locked/closed — not resolved)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Custom governance-freshness-check.ps1 running every 60 min via heartbeat. Alerts via Telegram when stale files found. Still required on v2026.2.26.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/35805
