---
layout: solution
title: "Agent Teams: teammates intermittently stop receiving SendMessage after extended polling (default mode, not tmux)"
category: general
source: https://github.com/anthropics/claude-code/issues/34668
---

# Agent Teams: teammates intermittently stop receiving SendMessage after extended polling (default mode, not tmux)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
** pressing `Esc` while viewing a stuck teammate's pane causes the previously-sent message to suddenly come through, and the teammate starts working. Without pressing Escape, the teammate remains idle indefinitely.

This is distinct from #23415 and its duplicates (#25254, #24771, #24108), which all describe the same symptom on the **tmux backend**. This issue occurs on the **default** (non-tmux) teammate mode, suggesting the inbox polling problem is not tmux-specific but affects the core message delivery/processing layer.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34668
