---
layout: solution
title: "fix(telegram): missing setStatus integration prevents health monitoring"
category: telegram
source: https://github.com/openclaw/openclaw/issues/32850
---

# fix(telegram): missing setStatus integration prevents health monitoring

## 증상
The Telegram provider lacks `setStatus` integration, causing `lastEventAt` and `lastInboundAt` timestamps to remain `null` even when messages are actively received. This prevents the channel health monitoring system from distinguishing between a functional quiet channel and a half-dead connection.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Thread `setStatus` callback from extension → monitor → bot/webhook, following the established Slack pattern (`Record<string, unknown>` signature). Add grammY middleware in `bot.ts` to call the callback on every inbound update, with try/catch error isolation.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/32850
