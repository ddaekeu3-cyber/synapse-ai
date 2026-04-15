---
layout: solution
title: "Doctor/update fails when Telegram bot token uses file-based SecretRef (2026.3 regression)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51810
description: "Regression (worked before, now"
---

# Doctor/update fails when Telegram bot token uses file-based SecretRef (2026.3 regression)

## 증상
Regression (worked before, now fails)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Wrap maintenance commands in a helper script that temporarily inlines the Telegram token, runs the command, then restores the SecretRef:
~/.openclaw/scripts/openclaw_safe.sh update
~/.openclaw/scripts/openclaw_safe.sh doctor
Inside the script we read the token from ~/.openclaw/secrets.json, set it inline in openclaw.json, run openclaw …, then write the SecretRef back. It works but defeats the purpose of keeping secrets file-only.

Request

Please add support for file-based SecretRefs in the doctor/update flows (or skip that check) so installations that keep their Telegram token in secrets.json

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51810
