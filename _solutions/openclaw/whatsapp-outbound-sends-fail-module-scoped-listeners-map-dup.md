---
layout: solution
title: "WhatsApp outbound sends fail: module-scoped listeners Map duplicated across 27 build chunks"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52781
description: "Outbound WhatsApp messages via the tool (proactive sends) fail"
---

# WhatsApp outbound sends fail: module-scoped listeners Map duplicated across 27 build chunks

## 증상
Outbound WhatsApp messages via the `message` tool (proactive sends) fail with:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Replace the module-scoped Map with a `globalThis`-shared Map in all affected chunks:

```diff
- const listeners = /* @__PURE__ */ new Map();
+ const listeners = globalThis.__oc_wa_listeners ??= new Map();
```

This needs to be applied to all files containing `#region src/web/active-listener.ts` (27 files in v2026.3.13).

One-liner to patch:
```bash
cd /usr/lib/node_modules/openclaw/dist
for f in *.js plugin-sdk/*.js; do
  LINENUM=$(grep -n '#region src/web/active-listener.ts' "$f" 2>/dev/null | head -1 | cut -d: -f1)
  if [ -n "$LINENUM" ]; then
    MAPLINE=$((LINENUM + 1))
    sed -i "${MAPLI

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52781
