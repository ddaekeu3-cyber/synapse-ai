---
layout: solution
title: "Configurable LLM request timeout per provider/model (Ollama cold-start causes silent fallback)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43946
---

# Configurable LLM request timeout per provider/model (Ollama cold-start causes silent fallback)

## 증상
When using local Ollama models, the first request after model load triggers a cold-start that takes **~13-46 seconds** (depending on model size). The default LLM request timeout in OpenClaw appears too short for this scenario, causing a **timeout-based fallback** (status 408) to the next model in the fallback chain — typically a cloud model.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Pre-load models before use via the Ollama API:

```bash
curl http://192.168.178.122:11434/api/generate \
  -d '{"model":"qwen3.5:122b","prompt":"hi","stream":false,"keep_alive":"60m","options":{"num_predict":1}}'
```

This forces the model into memory with a 60-minute keep-alive, avoiding cold-start timeouts on subsequent requests. However, this requires manual intervention or scripting before each session.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43946
