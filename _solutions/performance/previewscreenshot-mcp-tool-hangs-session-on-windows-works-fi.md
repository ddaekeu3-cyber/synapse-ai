---
layout: solution
title: "preview_screenshot MCP tool hangs session on Windows (works fine on macOS)"
category: performance
source: https://github.com/anthropics/claude-code/issues/30122
description: "When using Claude Code (v2.1.63) on Windows 11 via Claude Desktop, calling (and occasionally other preview MCP tools) causes the entire session to become"
---

# preview_screenshot MCP tool hangs session on Windows (works fine on macOS)

## 증상
When using Claude Code (v2.1.63) on Windows 11 via Claude Desktop, calling `preview_screenshot` (and occasionally other preview MCP tools) causes the entire session to become non-responsive. The session never recovers — I have to kill and restart every time.

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
Instructing Claude (via CLAUDE.md) to avoid `preview_screenshot` and use text-based alternatives (`preview_snapshot`, `preview_inspect`) instead. These text-based tools work reliably on Windows.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/30122
