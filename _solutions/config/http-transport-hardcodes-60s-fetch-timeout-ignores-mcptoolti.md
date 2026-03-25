---
layout: solution
title: "HTTP transport hardcodes 60s fetch timeout, ignores MCP_TOOL_TIMEOUT"
category: config
source: https://github.com/anthropics/claude-code/issues/36221
---

# HTTP transport hardcodes 60s fetch timeout, ignores MCP_TOOL_TIMEOUT

## 증상
The HTTP MCP transport has a hardcoded 60-second `AbortSignal.timeout()` on fetch requests that is **independent of and ignores** the `MCP_TOOL_TIMEOUT` environment variable. MCP tool calls that take longer than 60 seconds return empty `{}` responses silently — no error, no timeout message.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
None currently viable:
- `MCP_TIMEOUT` — only controls server startup timeout
- `MCP_TOOL_TIMEOUT` — controls protocol timeout but HTTP fetch aborts first
- Patching the minified binary is fragile and lost on updates

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36221
