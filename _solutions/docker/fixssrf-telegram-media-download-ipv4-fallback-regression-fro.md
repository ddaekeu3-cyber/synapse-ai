---
layout: solution
title: "fix(ssrf): Telegram media download IPv4 fallback regression from 45b74fb56c"
category: docker
source: https://github.com/openclaw/openclaw/issues/43464
description: "Telegram media downloads (PDFs, images, voice, etc.) fail with in dual-stack environments where IPv6 is enabled but has no internet route (common in"
---

# fix(ssrf): Telegram media download IPv4 fallback regression from 45b74fb56c

## 증상
Telegram media downloads (PDFs, images, voice, etc.) fail with `TypeError: fetch failed → AggregateError [ETIMEDOUT]` in dual-stack environments where IPv6 is enabled but has no internet route (common in Docker/container deployments).

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
`createPinnedDispatcher` should default to `autoSelectFamily: false`. Pinned dispatchers pre-resolve DNS via `resolvePinnedHostnameWithPolicy()` and `dedupeAndPreferIpv4()`, making Happy Eyeballs address selection redundant. Setting `autoSelectFamily: false` prevents IPv6-first connection attempts that timeout before IPv4 can succeed.

Also extends `createPinnedDispatcher` to accept optional `connectOptions` for callers that need explicit control over address family selection.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43464
