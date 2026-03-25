---
layout: solution
title: "All Google Chat webhooks return 404 after 2026.3.12 update (jiti/ESM registry split)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45544
---

# All Google Chat webhooks return 404 after 2026.3.12 update (jiti/ESM registry split)

## 증상
After updating to **2026.3.12**, all Google Chat webhook paths (`/googlechat-bender`, `/googlechat-eliza`, etc.) return 404. The gateway logs show all accounts starting successfully (`[googlechat] [bender] starting Google Chat webhook`), but every inbound request returns 404.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Patch 3 files to use a shared `globalThis.__allRegistriesPool` that collects all ever-activated registries, then aggregate `httpRoutes` across all pool members in the handler:

```js
// In setActivePluginRegistry (both registry files):
(globalThis.__allRegistriesPool ??= new Set()).add(registry);

// In createGatewayPluginRequestHandler:
const __pool = globalThis.__allRegistriesPool ?? new Set([registry]);
const __allRoutes = [...__pool].flatMap(r => r.httpRoutes ?? []);
if (__allRoutes.length === 0) return false;
const matchedRoutes = findMatchingPluginHttpRoutes({ httpRoutes: __allRoutes }, 

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45544
