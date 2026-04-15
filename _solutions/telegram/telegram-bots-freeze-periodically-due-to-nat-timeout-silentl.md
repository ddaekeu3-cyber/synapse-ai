---
layout: solution
title: "Telegram bots freeze periodically due to NAT timeout silently dropping idle getUpdates TCP connections"
category: telegram
source: https://github.com/openclaw/openclaw/issues/49461
description: "Telegram bots in OpenClaw periodically stop responding for ~15–90 seconds because the long-polling () TCP connection is silently dropped by the local NAT"
---

# Telegram bots freeze periodically due to NAT timeout silently dropping idle getUpdates TCP connections

## 증상
Telegram bots in OpenClaw periodically stop responding for ~15–90 seconds because the long-polling (`getUpdates`) TCP connection is silently dropped by the local NAT device. The stall repeats every ~1000 s (exact interval matches the router's NAT idle timeout). A fix is available in PR #49460.

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
See PR #49460: add `keepAlive: true, keepAliveInitialDelay: 30_000` to `buildTelegramConnectOptions`. A 30 s initial delay avoids unnecessary probes on short API calls while refreshing NAT entries well before typical expiry.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49461
