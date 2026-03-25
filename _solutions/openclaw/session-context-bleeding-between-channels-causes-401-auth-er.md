---
layout: solution
title: "Session context bleeding between channels causes 401 auth errors"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/22003
---

# Session context bleeding between channels causes 401 auth errors

## 증상
When using both webchat and Telegram simultaneously, session context bleeds between channels causing authentication failures and tool execution errors.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Disable one channel (either webchat or Telegram DMs).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/22003
