---
layout: solution
title: "Control UI chat header model picker sends bare model ids for provider-backed models"
category: docker
source: https://github.com/openclaw/openclaw/issues/47620
description: "Regression (worked before, now"
---

# Control UI chat header model picker sends bare model ids for provider-backed models

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #47620에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
- Use provider-qualified refs for chat header picker option values when the model comes from a provider-backed catalog entry
- For example, use `provider/id` instead of only `id`

This would make the chat header picker consistent with backend/config expectations.

Additional note:
Some model ids may already contain `/` as part of the provider-native model id (for example Docker-style model ids), so the UI should not assume that any value containing `/` is already an OpenClaw provider-qualified r

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47620
