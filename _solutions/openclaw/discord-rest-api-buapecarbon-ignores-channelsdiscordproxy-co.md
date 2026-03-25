---
layout: solution
title: "Discord REST API (@buape/carbon) ignores channels.discord.proxy config"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/30221
---

# Discord REST API (@buape/carbon) ignores channels.discord.proxy config

## 증상
Discord REST API calls via `@buape/carbon`'s `RequestClient` do not use the `channels.discord.proxy` configuration from `openclaw.json`. Only the WebSocket gateway connection respects the proxy setting.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using `NODE_OPTIONS=--require=proxy-preload.cjs` to call `setGlobalDispatcher(new EnvHttpProxyAgent())` from undici. This patches the fetch dispatcher without affecting `ws` library's raw TCP/TLS connections.

**Important**: Do NOT use `NODE_OPTIONS=--use-env-proxy` — it intercepts all connections including WebSocket upgrades via `http.request()`/`https.request()`, causing Discord WebSocket to disconnect with code 1005/1006.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30221
