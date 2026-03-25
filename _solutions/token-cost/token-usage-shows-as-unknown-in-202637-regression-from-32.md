---
layout: solution
title: "Token usage shows as 'unknown' in 2026.3.7 (regression from 3.2)"
category: token-cost
source: https://github.com/openclaw/openclaw/issues/39620
---

# Token usage shows as 'unknown' in 2026.3.7 (regression from 3.2)

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: token-cost.

## 해결법
Downgrade to OpenClaw 2026.3.2:

pnpm remove -g openclaw
pnpm install -g openclaw@2026.3.2
Possible Causes
ContextEngine plugin interface changes in 3.7 may have broken usage reporting
kimi-k2.5 model usage parsing may have regressed
Status display formatting may not be reading usage data correctly despite API success
Requested Fix
Restore token usage display in session_status and openclaw sessions output for 2026.3.7+.

Labels: bug, regression, token-usage, kimi-k2.5, 2026.3.7
Priority: Medium (affects cost monitoring)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39620
