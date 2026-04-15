---
layout: solution
title: "memory-lancedb-pro and memory-openviking plugin config rejected by gateway validator"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49495
description: "The and plugins' fields are rejected by OpenClaw's gateway-level config validator with \"must NOT have additional properties\" error, preventing the gateway"
---

# memory-lancedb-pro and memory-openviking plugin config rejected by gateway validator

## 증상
The `memory-lancedb-pro` and `memory-openviking` plugins' `config` fields are rejected by OpenClaw's gateway-level config validator with "must NOT have additional properties" error, preventing the gateway from starting when these configs are present in `openclaw.json`.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
**Current workaround** (loses all custom configuration):

1. Backup plugin configs to a separate file
2. Remove `config` fields from `plugins.entries.*` in `openclaw.json`
3. Keep only `enabled: true`
4. Restart gateway

This allows the gateway to start but **memory plugins run with default settings only**, losing:
- Custom embedding providers and API endpoints
- Database paths
- Auto-capture/recall settings
- Retrieval modes and reranking
- Memory scopes and agent access controls

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49495
