---
layout: solution
title: "[Feature Request] Exec approval should only show on the triggering agent's Telegram channel"
category: telegram
source: https://github.com/openclaw/openclaw/issues/53409
---

# [Feature Request] Exec approval should only show on the triggering agent's Telegram channel

## 증상
When an exec command requires approval, the approval request is broadcast to ALL connected Telegram bots/channels instead of just the one that triggered it.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Using `ask: "off"` to bypass approvals, but this is not ideal for security.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53409
