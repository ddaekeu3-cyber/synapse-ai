---
layout: solution
title: "Plugin runtime self-update writes timestamps to openclaw.json, triggering gateway stop (not restart loop)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49474
description: "When an npm-installed plugin (e.g. ) performs a runtime self-update (checking npm for a newer version and installing it while the gateway is running), it"
---

# Plugin runtime self-update writes timestamps to openclaw.json, triggering gateway stop (not restart loop)

## 증상
When an npm-installed plugin (e.g. `lossless-claw`) performs a **runtime self-update** (checking npm for a newer version and installing it while the gateway is running), it writes new `resolvedAt` and `installedAt` timestamps to `openclaw.json`. This triggers the `plugins → restart` reload rule, causing the gateway to send itself SIGTERM and **stop entirely** — without recovering.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Set `gateway.reload.mode = "hot"` in `openclaw.json`. This suppresses the restart trigger, but means plugin updates are silently ignored until manual restart — not ideal for a plugin acting as `contextEngine`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49474
