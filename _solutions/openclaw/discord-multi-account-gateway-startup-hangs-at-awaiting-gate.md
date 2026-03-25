---
layout: solution
title: "Discord: multi-account gateway startup hangs at 'awaiting gateway readiness' after Carbon reconcile change (v2026.3.22)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53132
---

# Discord: multi-account gateway startup hangs at 'awaiting gateway readiness' after Carbon reconcile change (v2026.3.22)

## 증상
Running 4 Discord bot accounts on a single gateway causes 2–4 bots to hang indefinitely at `client initialized as <id> (<name>); awaiting gateway readiness` on every restart. The issue appeared after upgrading from v2026.3.13 to v2026.3.22 with the same configuration and bot tokens. v2026.3.13 starts all 4 bots reliably.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Rolling back to v2026.3.13: `openclaw update --tag v2026.3.13`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53132
