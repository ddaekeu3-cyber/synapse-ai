---
layout: solution
title: "Gateway crash corrupts session history: thinking blocks become unrecoverable"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/25194
---

# Gateway crash corrupts session history: thinking blocks become unrecoverable

## 증상
When the OpenClaw gateway crashes while a session has thinking mode enabled (`thinking=low` or higher), the session becomes permanently unrecoverable. Every subsequent message returns:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
— users will retry, get the same error, and be stuck in a loop
- This is a reliability/safety issue: any unclean gateway shutdown can silently corrupt sessions

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/25194
