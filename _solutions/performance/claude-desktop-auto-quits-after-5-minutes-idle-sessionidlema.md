---
layout: solution
title: "Claude Desktop auto-quits after 5 minutes idle (SessionIdleManager 300s timeout)"
category: performance
source: https://github.com/anthropics/claude-code/issues/23092
---

# Claude Desktop auto-quits after 5 minutes idle (SessionIdleManager 300s timeout)

## 증상
Claude Desktop automatically terminates after approximately 5 minutes of inactivity due to a `SessionIdleManager` with a hardcoded 300-second timeout. This behavior can interrupt ongoing background processes, agent work, or long-running operations.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Currently none available. The `claude_desktop_config.json` does not appear to have an option to configure or disable the idle timeout.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/23092
