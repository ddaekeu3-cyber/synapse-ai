---
layout: solution
title: "Telegram provider stale-socket restart causes stuck message replay loop"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/35959
---

# Telegram provider stale-socket restart causes stuck message replay loop

## 증상
Long-running agent sessions cause the Telegram provider to enter a stale-socket → restart → re-deliver loop, sending the same message to the user every 10 minutes indefinitely.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
`launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway` to restart the gateway and clear the stuck session state.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/35959
