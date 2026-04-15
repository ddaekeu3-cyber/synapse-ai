---
layout: solution
title: "Plugin HTTP route dispatch uses stale registry - webhook POST returns 404"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48734
description: "BlueBubbles (and likely other channel plugins) webhook HTTP routes return 404 despite being correctly registered during gateway startup. The route IS"
---

# Plugin HTTP route dispatch uses stale registry - webhook POST returns 404

## 증상
BlueBubbles (and likely other channel plugins) webhook HTTP routes return 404 despite being correctly registered during gateway startup. The route IS registered and appears in logs (`BlueBubbles webhook listening on /bluebubbles-webhook`), but POST requests fall through to the 404 fallback.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Patch `createGatewayPluginRequestHandler` and `shouldEnforcePluginGatewayAuth` in the active `gateway-cli-*.js` to read the global active registry at request time:

```javascript
// Instead of:
const { registry, log } = params;

// Use:
const _getRegistry = () => {
  const _s = globalThis[Symbol.for("openclaw.pluginRegistryState")];
  return _s?.registry ?? params.registry;
};
// Then: const registry = _getRegistry(); at request time
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48734
