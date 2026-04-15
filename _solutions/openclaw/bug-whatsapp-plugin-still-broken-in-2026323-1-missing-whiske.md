---
layout: solution
title: "Bug: WhatsApp plugin still broken in 2026.3.23-1 — missing @whiskeysockets/baileys dependency"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53285
description: "fixed the missing / from #53244, but the WhatsApp plugin still fails at runtime because its npm dependency is not bundled in the"
---

# Bug: WhatsApp plugin still broken in 2026.3.23-1 — missing @whiskeysockets/baileys dependency

## 증상
`openclaw@2026.3.23-1` fixed the missing `light-runtime-api.js` / `runtime-api.js` from #53244, but the WhatsApp plugin still fails at runtime because its npm dependency `@whiskeysockets/baileys` is not bundled in the package.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
We run `npm install --omit=dev` inside `dist/extensions/whatsapp/` (after removing `workspace:*` refs from package.json) during our Docker asset build to inject the missing dependencies manually.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53285
