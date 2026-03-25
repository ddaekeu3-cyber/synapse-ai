---
layout: solution
title: "Queue inbound Discord messages during WS reconnect window (~22s post-boot gap)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52577
---

# Queue inbound Discord messages during WS reconnect window (~22s post-boot gap)

## 증상
After a gateway restart, there's a ~22-second window where the HTTP server and cron scheduler are running but the Discord WebSocket isn't connected yet. Messages sent by users during this window are silently dropped — no error, no retry, no indication to the user that the bot didn't receive their message.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Users can wait ~30 seconds after a restart before messaging, but this isn't discoverable and relies on the user knowing the bot just restarted.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52577
