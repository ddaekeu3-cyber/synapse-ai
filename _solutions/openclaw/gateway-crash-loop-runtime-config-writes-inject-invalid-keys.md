---
layout: solution
title: "Gateway crash loop: runtime config writes inject invalid keys (groupAllowFrom, allowFrom, routing, allowlist)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/29780
---

# Gateway crash loop: runtime config writes inject invalid keys (groupAllowFrom, allowFrom, routing, allowlist)

## 증상
The gateway writes keys (`groupAllowFrom`, `allowFrom`, `routing`, `allowlist`) into `~/.openclaw/openclaw.json` at runtime when processing Discord messages, but the config validator in v2026.2.26 rejects those same keys as "Unrecognized". This causes a crash loop:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Remove the invalid keys and make the config file immutable so the gateway cannot write them back:

```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/29780
