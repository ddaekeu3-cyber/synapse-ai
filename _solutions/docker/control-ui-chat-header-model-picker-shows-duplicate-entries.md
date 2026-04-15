---
layout: solution
title: "Control UI chat header model picker shows duplicate entries for normalized model refs"
category: docker
source: https://github.com/openclaw/openclaw/issues/47621
description: "Regression (worked before, now"
---

# Control UI chat header model picker shows duplicate entries for normalized model refs

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #47621에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
- Normalize bare values against the model catalog before deduplication
- For example, normalize `gpt-5.4` to `openai-codex/gpt-5.4` if that catalog entry exists
- Keep the dedicated `Default (...)` option, but avoid duplicate logical model entries underneath it

Additional note:
This also affects model ids that already contain slashes as part of the model id itself (for example Docker-style ids like `docker.io/ai/gpt-oss:latest`).

So checking `value.includes("/")` is not sufficient to determine

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47621
