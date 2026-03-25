---
layout: solution
title: "Telegram responses not sent - missing 'run done' event"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51659
---

# Telegram responses not sent - missing "run done" event

## 증상
Embedded runs complete successfully (agent end, prompt end) but the "run done" event is not emitted in some cases, causing Telegram responses to not be sent to users. Users only see "typing" indicator but never receive the actual response.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- Restart Gateway: `openclaw gateway restart`
- Manually send response via Telegram Bot API

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51659
