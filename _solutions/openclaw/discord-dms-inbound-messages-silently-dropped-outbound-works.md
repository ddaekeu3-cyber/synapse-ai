---
layout: solution
title: "Discord DMs: inbound messages silently dropped (outbound works)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48641
---

# Discord DMs: inbound messages silently dropped (outbound works)

## 증상
Discord inbound DMs from an allowlisted user are silently dropped by the gateway. Outbound DMs (bot → user) work correctly. Guild channel messages work correctly.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using Discord guild channels (#general, #aria) for interactive work + Telegram for private DM conversations. Functional but fragmented.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48641
