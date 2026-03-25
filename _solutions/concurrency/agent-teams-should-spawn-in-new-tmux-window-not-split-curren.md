---
layout: solution
title: "Agent teams should spawn in new tmux window, not split current pane"
category: concurrency
source: https://github.com/anthropics/claude-code/issues/23615
---

# Agent teams should spawn in new tmux window, not split current pane

## 증상
Agent teams created via `Task` tool with `team_name` spawn new tmux panes by splitting the current window. This breaks the user's existing layout and causes command corruption when multiple agents start simultaneously.

## 원인
보고된 버그/문제. 카테고리: concurrency.

## 해결법
Dispatch fewer agents (2 instead of 4), or handle crashed agents' tasks in the main session.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/23615
