---
layout: solution
title: "CLI commands fail with handshake timeout on arm64 (Raspberry Pi 5)"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/46097
description: "Behavior bug (incorrect output/state without"
---

# CLI commands fail with handshake timeout on arm64 (Raspberry Pi 5)

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
Model generated plausible but incorrect output due to insufficient grounding, missing verification, or high sampling temperature.

## 해결법
Local patch to gateway dist files that adds `OPENCLAW_HANDSHAKE_TIMEOUT_MS` env var support to `getHandshakeTimeoutMs()`, combined with a systemd drop-in override setting the value to 15000ms. This must be reapplied after every `npm install -g openclaw` upgrade.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46097
