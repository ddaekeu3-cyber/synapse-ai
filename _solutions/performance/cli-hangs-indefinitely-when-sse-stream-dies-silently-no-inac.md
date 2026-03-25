---
layout: solution
title: "CLI hangs indefinitely when SSE stream dies silently — no inactivity watchdog"
category: performance
source: https://github.com/anthropics/claude-code/issues/37080
---

# CLI hangs indefinitely when SSE stream dies silently — no inactivity watchdog

## 증상
When a streaming SSE connection dies silently (no TCP RST packet), the CLI **hangs indefinitely**. There is no watchdog timer monitoring the SSE stream for event gaps. Common triggers: NAT timeout during extended thinking, silent Wi-Fi disconnect, load balancer idle timeout. Users must kill the process manually.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
is ~40 lines with clear boundaries. Pairs naturally with the connection retry fix (issue #37077) for automatic recovery.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37080
