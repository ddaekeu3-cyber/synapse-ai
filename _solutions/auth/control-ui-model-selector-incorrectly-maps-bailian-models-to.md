---
layout: solution
title: "Control UI: Model selector incorrectly maps bailian models to qwen-portal provider"
category: auth
source: https://github.com/openclaw/openclaw/issues/51824
---

# Control UI: Model selector incorrectly maps bailian models to qwen-portal provider

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #51824에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
After selecting the bailian model, click "Refresh Chat Data" to reset the UI state. The model then works correctly.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51824
