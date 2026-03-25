---
layout: solution
title: "[Critical] LINE/Telegram webhook routes silently unregistered — all inbound messages dropped (404)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48712
---

# [Critical] LINE/Telegram webhook routes silently unregistered — all inbound messages dropped (404)

## 증상
**All LINE webhook POST requests return 404**, causing the bot to silently drop every inbound message. The gateway shows LINE providers as "running" and "configured" in status/doctor output, giving no indication that webhooks are completely broken. This has been present since at least `2026.3.12` through `2026.3.13`.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Patch `dist/gateway-cli-*.js` in `createGatewayPluginRequestHandler` to read from `globalThis[Symbol.for("openclaw.pluginRegistryState")]` instead of the closure-captured `registry`. This survives restarts but is overwritten on package update.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48712
