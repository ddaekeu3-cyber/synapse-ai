---
layout: solution
title: "Gateway crashes on startup when logging.file contains tilde path"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/30401
---

# Gateway crashes on startup when logging.file contains tilde path

## 증상
When `logging.file` is set to a tilde-prefixed path (e.g. `~/.openclaw/logs/gateway.log`) in `openclaw.json`, the gateway crashes on startup with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Call `expandHomePrefix()` on the file path in `resolveSettings()`:

```ts
import { expandHomePrefix } from "../infra/home-dir.js";
// ...
const rawFile = cfg?.file ?? defaultRollingPathForToday();
const file = expandHomePrefix(rawFile);
```

This aligns with how other config paths are normalized via `normalizeConfigPaths()`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30401
