---
layout: solution
title: "WhatsApp: support pre-built link previews in sendMessage + batch send RPC method"
category: general
source: https://github.com/openclaw/openclaw/issues/50195
---

# WhatsApp: support pre-built link previews in sendMessage + batch send RPC method

## 증상
When sending multiple WhatsApp messages programmatically (e.g., from cron jobs or digest workflows), there are two pain points:

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
We wrote custom scripts that:
1. Pre-fetch all previews in parallel via Baileys' `getUrlInfo` (pure HTTP)
2. Write to a temp JSON file (`/tmp/wa_previews.json`)
3. Patch `channel-web` to check for the temp file and inject `linkPreview` into the Baileys payload
4. Use `callGatewayFromCli` in a loop within a single Node.js process to avoid per-message CLI boot overhead

This works but requires patching dist files after every update.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50195
