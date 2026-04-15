---
layout: solution
title: "Support Anthropic 1M context window for Amazon Bedrock provider"
category: context-window
source: https://github.com/openclaw/openclaw/issues/31257
description: "The option currently only works with the direct Anthropic API provider (). When using Amazon Bedrock via the Converse API, the 1M context window beta ()"
---

# Support Anthropic 1M context window for Amazon Bedrock provider

## 증상
The `context1m: true` option currently only works with the direct Anthropic API provider (`provider === "anthropic"`). When using Amazon Bedrock via the Converse API, the 1M context window beta (`context-1m-2025-08-07`) requires injecting `additionalModelRequestFields` with `anthropic_beta`, which OpenClaw does not currently support.

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
Manual patch of `@mariozechner/pi-ai/dist/providers/amazon-bedrock.js`:
1. Strip `[1m]` suffix from `modelId` before sending to Bedrock
2. When model ID contains `[1m]`, inject `additionalModelRequestFields: { anthropic_beta: ["context-1m-2025-08-07"] }` into the Converse API call

This patch must be reapplied after every `npm update`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31257
