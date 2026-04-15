---
layout: solution
title: "bug(whatsapp): applyGroupGating ignores accountId — multi-account group allowlist bypass"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/40019
description: "in the WhatsApp channel does not pass to , causing it to resolve against the account instead of the actual account (e.g., ). In multi-account setups, this"
---

# bug(whatsapp): applyGroupGating ignores accountId — multi-account group allowlist bypass

## 증상
`applyGroupGating` in the WhatsApp channel does not pass `accountId` to `resolveGroupPolicyFor`, causing it to resolve against the `"default"` account instead of the actual account (e.g., `"openclaw"`). In multi-account setups, this means the group allowlist check reads the **wrong account config**, allowing messages in unauthorized groups.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Patch `channel-web-*.js` directly (identify correct file via `grep "channel-web" dist/index.js`). Hard restart required (SIGUSR1 does not reload the file; must `kill -9` the old PID).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40019
