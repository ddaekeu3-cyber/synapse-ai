---
layout: solution
title: "TELEGRAM_BOT_TOKEN env override is ignored on doctor/update when config uses exec SecretRef"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52344
description: "and still fail on macOS when is an SecretRef, even if is already exported in the shell before the command"
---

# TELEGRAM_BOT_TOKEN env override is ignored on doctor/update when config uses exec SecretRef

## 증상
`openclaw doctor` and `openclaw update` still fail on macOS when `channels.telegram.botToken` is an `exec` SecretRef, even if `TELEGRAM_BOT_TOKEN` is already exported in the shell before the command runs.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Current local workaround is to bypass `openclaw update` entirely:

1. `pnpm add -g openclaw@latest`
2. `openclaw plugins update --all`
3. `openclaw completion --write-state`
4. `openclaw gateway restart`
5. `openclaw gateway status --json`
6. `openclaw channels status --probe`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52344
