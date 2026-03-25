---
layout: solution
title: "Bug: anthropic:manual sanitize-on-retry strips tool_use but leaves orphaned tool_result → second 400 loop"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/40433
---

# Bug: anthropic:manual sanitize-on-retry strips tool_use but leaves orphaned tool_result → second 400 loop

## 증상
When the session transcript contains tool calls from a **non-Anthropic provider** (e.g. OpenAI Codex, call_id format `call_...` or `callXYZ...`) and Anthropic is the **fallback provider**, the `anthropic:manual` profile's sanitize-on-retry path strips the malformed `tool_use` blocks but leaves the corresponding `tool_result` blocks orphaned. This causes a second rejection that OpenClaw cannot reco

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
- Start a new session (`/new`) to clear the poisoned transcript
- **Avoid cross-provider fallback chains** — keep fallbacks same-provider-only to prevent transcript contamination

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40433
