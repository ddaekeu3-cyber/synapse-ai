---
layout: solution
title: "iMessage inbound messages not received on v2026.3.x — BlueBubbles webhook route returns 404 after upgrade"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52605
description: "Regression (worked before, now"
---

# iMessage inbound messages not received on v2026.3.x — BlueBubbles webhook route returns 404 after upgrade

## 증상
Regression (worked before, now fails)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
did not resolve the issue — v2026.3.2 through v2026.3.13 all exhibit the same 404 behavior on the BlueBubbles webhook endpoint, and iMessage inbound delivery remains broken across the entire v2026.3.x release line.

Suspect area: `registerPluginHttpRoute` in `src/plugins/http-registry.ts` registers the BlueBubbles webhook route into the plugin registry, but `createGatewayHttpServer` in `src/gateway/server/gateway-http.ts` calls `buildPluginRequestStages` → `handlePluginRequest` → `findMatchingPluginHttpRoutes`, which may be referencing a stale or different registry instance after gateway resta

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52605
