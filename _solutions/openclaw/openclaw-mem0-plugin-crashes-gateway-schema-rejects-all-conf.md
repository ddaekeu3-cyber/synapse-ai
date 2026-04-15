---
layout: solution
title: "openclaw-mem0 plugin crashes gateway — schema rejects all config keys except 'enabled'"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43551
description: "Regression (worked before, now"
---

# openclaw-mem0 plugin crashes gateway — schema rejects all config keys except "enabled"

## 증상
Regression (worked before, now fails)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
of using a nested config key also fails validation
Version: OpenClaw 2026.3.8 (3caab92)

plugins.entries.openclaw-mem0 schema validation rejects all keys except "enabled" — even though the plugin's own ALLOWED_KEYS includes mode, userId, autoRecall, oss etc. The workaround of using a nested "config" key also fails validation. Gateway exits immediately with code 1 on every start. Only workaround is keeping openclaw-mem0 entry as {"enabled": true} with no config, which loads the plugin without Qdrant/self-hosted settings. Version: OpenClaw 2026.3.8 (3caab92), macOS, installed via Homebrew.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43551
