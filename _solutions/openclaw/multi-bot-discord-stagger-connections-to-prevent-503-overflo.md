---
layout: solution
title: "Multi-bot Discord: stagger connections to prevent 503 overflow on startup"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/44258
---

# Multi-bot Discord: stagger connections to prevent 503 overflow on startup

## 증상
When running multiple Discord bot accounts (4-5 bots) via `channels.discord.accounts`, all bots attempt to connect simultaneously on gateway startup/restart. This floods the Discord API and consistently produces:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Reducing to 2-3 Discord bots makes the problem much less likely. The fundamental issue is concurrent connections, not the bot count per se.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44258
