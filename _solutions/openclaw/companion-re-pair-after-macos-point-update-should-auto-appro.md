---
layout: solution
title: "Companion re-pair after macOS point update should auto-approve when isRepair=true and deviceId+publicKey match"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/36973
---

# Companion re-pair after macOS point update should auto-approve when isRepair=true and deviceId+publicKey match

## 증상
After a macOS point update (26.3.0 → 26.3.1), the OpenClaw companion app sends a re-pair request to the gateway with `isRepair: true`, the same `deviceId`, and the same `publicKey`. The only change is the `platform` string.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Manually update `~/.openclaw/devices/paired.json` to change the `platform` field to the new version, clear `~/.openclaw/devices/pending.json`, and restart the gateway.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/36973
