---
layout: solution
title: "Slot plugin tools not exposed to Agent due to Rollup code-splitting state isolation"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/48919
description: "Behavior bug (incorrect output/state without"
---

# Slot plugin tools not exposed to Agent due to Rollup code-splitting state isolation

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
Model generated plausible but incorrect output due to insufficient grounding, missing verification, or high sampling temperature.

## 해결법
Use CLI commands as an alternative:
```bash
openclaw memory-pro search "query"     # Search memories
openclaw memory-pro list               # List all memories
openclaw memory-pro stats              # Show statistics
```

However, this bypasses the Agent's ability to proactively manage memories during conversations.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48919
