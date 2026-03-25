---
layout: solution
title: "MCP Tool: gemini-research — saves ~95% of research tokens"
category: context-window
source: https://github.com/anthropics/claude-code/issues/27234
---

# MCP Tool: gemini-research — saves ~95% of research tokens

## 증상
Every time Claude Code uses `WebSearch` or `WebFetch`, it dumps **5,000 to 20,000+ tokens** of raw HTML/markdown into the context window. On a research-heavy session (checking versions, looking up docs, comparing tools), you can burn through 50,000–100,000 tokens just on web searches — leaving less room for actual coding work.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
I built **[gemini-research](https://github.com/MichaelGagnon13/gemini-research)** — an MCP server + CLI tool that delegates web research to Gemini's free API (250 requests/day with Google Search grounding). Instead of raw web pages, Claude Code gets back **~200–400 focused tokens** per query.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/27234
