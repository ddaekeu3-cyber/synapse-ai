---
layout: solution
title: "Azure OpenAI models report 0 context tokens — supportsUsageInStreaming forcefully disabled"
category: general
source: https://github.com/openclaw/openclaw/issues/38784
---

# Azure OpenAI models report 0 context tokens — supportsUsageInStreaming forcefully disabled

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
was correct for generic backends but did not account for Azure OpenAI, which fully supports `stream_options: { include_usage: true }`.

**Fix**: Add `isAzureOpenAIEndpoint()` check matching `host.endsWith(".openai.azure.com")`. For Azure endpoints, only force `supportsDeveloperRole: false` (Azure rejects `developer` role) but preserve `supportsUsageInStreaming` (default `true`).

Config-only workaround (setting `compat.supportsUsageInStreaming: true` in `models.json`) does NOT work because `normalizeModelCompat` explicitly overrides user-set values for non-native endpoints.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/38784
