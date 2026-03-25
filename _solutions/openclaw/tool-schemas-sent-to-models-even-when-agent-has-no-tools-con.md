---
layout: solution
title: "Tool schemas sent to models even when agent has no tools configured"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/30004
---

# Tool schemas sent to models even when agent has no tools configured

## 증상
When a cron job runs against an agent with no `tools` configured (e.g. `heartbeat-bot`), the gateway still appears to send tool/function schemas to the model. This causes small local models (via Ollama) to fail with errors, even though they can respond correctly when called directly via the OpenAI-compatible API without tools.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Use a larger model that can handle the tool schemas, or use a model on a provider that strips unused schemas.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30004
