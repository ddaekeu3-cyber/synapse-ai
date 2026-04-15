---
layout: solution
title: "2026.3.23-2: Telegram channel stops initializing permanently after polling stall loop"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53493
description: "Severity: Critical — Telegram channel permanently stops initializing after polling stall loop escalates, surviving reboots. Downgrading to immediately"
---

# 2026.3.23-2: Telegram channel stops initializing permanently after polling stall loop

## 증상
**Severity: Critical** — Telegram channel permanently stops initializing after polling stall loop escalates, surviving reboots. Downgrading to `2026.3.22` immediately fixes the issue.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
**Downgrade to 2026.3.22 immediately resolves the issue:**
```
npm install -g openclaw@2026.3.22
openclaw gateway restart
```
After downgrade, `gateway/channels/telegram [default] starting provider` reappears in logs immediately and messages are received normally.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53493
