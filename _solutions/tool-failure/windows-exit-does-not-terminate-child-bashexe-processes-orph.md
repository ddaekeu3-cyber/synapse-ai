---
layout: solution
title: "Windows: /exit does not terminate child bash.exe processes — orphaned shells cause system-wide slowdown"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/32183
---

# Windows: /exit does not terminate child bash.exe processes — orphaned shells cause system-wide slowdown

## 증상
On Windows, when a Claude Code session that has executed tool calls is exited via `/exit`, the child `bash.exe` processes spawned during the session are **not terminated**. These orphaned shells continue spawning and dying at a rate of 5-8 processes every 2-3 seconds indefinitely, even though no task is running and Claude has printed "See ya!" and returned the shell prompt.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
(closing the terminal window) is not obvious and users may leave terminal windows open, accumulating orphaned shell processes across multiple sessions.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32183
