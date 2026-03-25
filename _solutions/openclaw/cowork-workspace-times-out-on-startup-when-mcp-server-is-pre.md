---
layout: solution
title: "Cowork workspace times out on startup when MCP server is present in config (Windows 11 Home)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/35511
---

# Cowork workspace times out on startup when MCP server is present in config (Windows 11 Home)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Remove the MCP entry from config — but this means choosing between Cowork and MCP in Chat. No way to have both.

Additional context
This is similar to issues #24918, #25206, and #27801. The MCP server runs successfully standalone and works in Chat mode — the timeout appears specific to Cowork's VM attempting to reach or initialise host-side MCP servers on startup.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/35511
