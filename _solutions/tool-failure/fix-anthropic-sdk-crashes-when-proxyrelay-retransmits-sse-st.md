---
layout: solution
title: "fix: Anthropic SDK crashes when proxy/relay retransmits SSE stream — patch & workaround"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/39454
description: "When using OpenClaw with a third-party API proxy/relay (e.g., nexus-style relay), the bot intermittently fails to respond on complex queries (tool calls,"
---

# fix: Anthropic SDK crashes when proxy/relay retransmits SSE stream — patch & workaround

## 증상
When using OpenClaw with a third-party API proxy/relay (e.g., nexus-style relay), the bot intermittently fails to respond on complex queries (tool calls, web search). The root cause is in `@anthropic-ai/sdk@0.73.0`'s `MessageStream._accumulateMessage()`, which throws a fatal error when the relay retransmits an SSE stream after an upstream interruption.

## 원인
Tool or plugin call failed due to schema mismatch, missing parameter, permission error, or upstream API change. 카테고리: tool-failure.

## 해결법
Patch `@anthropic-ai/sdk`'s `MessageStream._accumulateMessage()` to **reset the snapshot instead of throwing** when a duplicate `message_start` arrives. This gracefully discards the incomplete first stream and continues processing the retransmitted stream.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39454
