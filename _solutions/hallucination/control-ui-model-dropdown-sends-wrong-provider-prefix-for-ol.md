---
layout: solution
title: "Control UI model dropdown sends wrong provider prefix for Ollama models"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/46764
---

# Control UI model dropdown sends wrong provider prefix for Ollama models

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
GitHub Issue #46764에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
3. Open Control UI (`/chat`)
4. Use the "Chat model" dropdown to select any Ollama model (e.g., `deepseek-r1:8b · ollama`)
5. Send a message

The dropdown displays models correctly (showing `deepseek-r1:8b · ollama`) but the model override sent to the gateway uses the wrong provider prefix.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46764
