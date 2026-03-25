---
layout: solution
title: "Agent Stop hook receives 'SubagentStop' instead of 'Stop' as hook_event_name"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/19220
---

# Agent Stop hook receives "SubagentStop" instead of "Stop" as hook_event_name

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
maps `SubagentStop` back to `Stop` before processing, but this shouldn't be necessary since the frontmatter defines `Stop:` not `SubagentStop:`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/19220
