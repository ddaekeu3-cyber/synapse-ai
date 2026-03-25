---
layout: solution
title: "Feature request: channels.discord.connectStagger config option"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/44400
---

# Feature request: channels.discord.connectStagger config option

## 증상
When the gateway starts with multiple Discord bot accounts (e.g., 9 agents), all bots connect simultaneously via `Promise.all()`. This overwhelms Discord's API with concurrent connections, triggering 503 'upstream overflow' errors. Failed bots then enter a 5-second auto-restart loop, creating bursts of retries that cause unhandled promise rejection crashes. When launchd restarts the gateway, anoth

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
to be re-applied manually after each update.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44400
