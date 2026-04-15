---
layout: solution
title: "v2.1.73 causes terminal freeze with yellow search bar in tmux sessions - memory leak related"
category: performance
source: https://github.com/anthropics/claude-code/issues/33350
description: "Claude Code v2.1.73 causes terminal to freeze with a yellow \"(search down)\" / \"(repeat)\" / \"(jump to forward)\" bar at the bottom, blocking all input. This"
---

# v2.1.73 causes terminal freeze with yellow search bar in tmux sessions - memory leak related

## 증상
Claude Code v2.1.73 causes terminal to freeze with a yellow "(search down)" / "(repeat)" / "(jump to forward)" bar at the bottom, blocking all input. This happens in tmux sessions on Ubuntu 24.04 server.

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
- Set `CLAUDE_CODE_DISABLE_AUTOUPDATE=1` to stay on v2.1.52
- Sessions on v2.1.52 do NOT exhibit this behavior

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33350
