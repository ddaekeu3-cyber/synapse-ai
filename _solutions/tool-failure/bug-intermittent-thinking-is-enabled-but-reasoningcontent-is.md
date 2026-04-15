---
layout: solution
title: "Bug: Intermittent 'thinking is enabled but reasoning_content is missing in assistant tool call message' error crashes session"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/37485
description: "During normal Claude Code sessions (using official Claude models), an intermittent API 400 error occurs that immediately terminates the current"
---

# Bug: Intermittent "thinking is enabled but reasoning_content is missing in assistant tool call message" error crashes session

## 증상
During normal Claude Code sessions (using official Claude models), an intermittent API 400 error occurs that immediately terminates the current operation:

## 원인
Tool or plugin call failed due to schema mismatch, missing parameter, permission error, or upstream API change. 카테고리: tool-failure.

## 해결법
```
/clear
```
Then re-describe the task from scratch.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37485
