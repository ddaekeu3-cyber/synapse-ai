---
layout: solution
title: "feat: make embedding batch timeout configurable for remote/local providers (Ollama)"
category: memory
source: https://github.com/openclaw/openclaw/issues/25157
description: "When using a local embedding provider served via Ollama with and pointing to an Ollama instance, the embedding batch timeout is hardcoded to 2 minutes"
---

# feat: make embedding batch timeout configurable for remote/local providers (Ollama)

## 증상
When using a local embedding provider served via Ollama with `provider: "openai"` and `remote.baseUrl` pointing to an Ollama instance, the embedding batch timeout is hardcoded to **2 minutes** (`EMBEDDING_BATCH_TIMEOUT_REMOTE_MS = 2 * 60000`).

## 원인
Agent session state was not persisted to durable storage, causing context to be lost on restart or session switch.

## 해결법
Manually patching `EMBEDDING_BATCH_TIMEOUT_REMOTE_MS` in the dist files after every update — not sustainable.

> Note: the `embedTimeoutMs` key exists in `MemoryQmdUpdateConfig` but that is for the QMD backend, not the builtin memory search backend.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/25157
