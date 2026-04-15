---
layout: solution
title: "Dashboard model selector incorrectly constructs provider/model ID when switching across providers"
category: config
source: https://github.com/openclaw/openclaw/issues/47380
description: "Regression (worked before, now"
---

# Dashboard model selector incorrectly constructs provider/model ID when switching across providers

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #47380에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
### Additional information

First known bad version: v2026.3.13. Temporary workaround: use `/model <alias>` command directly in the chat input (e.g. `/model glm47flash`) instead of the Dashboard dropdown — this correctly resolves the alias to the full `provider/model` path.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47380
