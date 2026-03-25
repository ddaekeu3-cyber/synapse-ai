---
layout: solution
title: "Feature request: Zero-token quick replies (pattern-matched auto-responses)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/44128
---

# Feature request: Zero-token quick replies (pattern-matched auto-responses)

## 증상
Add a `quickReplies` (or `autoResponses`) config option that lets agents define simple pattern → response mappings that bypass the LLM entirely. These would be handled at the gateway level with zero input/output tokens.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Defining the pattern in the system prompt (e.g., "if message is exactly Test, reply Here"). This works but still burns a full LLM turn with system prompt tokens on every match.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44128
