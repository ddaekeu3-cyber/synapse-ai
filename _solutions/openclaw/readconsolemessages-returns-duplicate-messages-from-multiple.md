---
layout: solution
title: "read_console_messages returns duplicate messages from multiple execution contexts"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/30591
---

# read_console_messages returns duplicate messages from multiple execution contexts

## 증상
The `read_console_messages` Chrome MCP tool returns duplicate console messages. A single `console.log("hello")` on a page can appear 2-3 times in the tool's output. This is because the Chrome DevTools Protocol (CDP) fires `Runtime.consoleAPICalled` once per **execution context** in the tab (main page, service worker, extension content scripts), and the tool doesn't filter by context.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using `read_network_requests` with `urlPattern` instead of `read_console_messages` for verifying API calls, and using server-side logs as ground truth for counting operations.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/30591
