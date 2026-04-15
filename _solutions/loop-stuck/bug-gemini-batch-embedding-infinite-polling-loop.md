---
layout: solution
title: "Bug: Gemini Batch Embedding Infinite Polling Loop"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/15738
description: "- OpenClaw Version: Latest (as of Feb"
---

# Bug: Gemini Batch Embedding Infinite Polling Loop

## 증상
- **OpenClaw Version:** Latest (as of Feb 2026)

## 원인
Agent entered a retry or decision loop without an exit condition, consuming tokens indefinitely without making progress. 카테고리: loop-stuck.

## 해결법
Users should disable batch mode for Gemini embeddings in config:

```json
{
  "memorySearch": {
    "provider": "gemini",
    "remote": {
      "batch": {
        "enabled": false
      }
    },
    "model": "gemini-embedding-001"
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/15738
