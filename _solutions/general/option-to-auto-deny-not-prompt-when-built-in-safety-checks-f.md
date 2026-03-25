---
layout: solution
title: "Option to auto-deny (not prompt) when built-in safety checks flag a command"
category: general
source: https://github.com/anthropics/claude-code/issues/28993
---

# Option to auto-deny (not prompt) when built-in safety checks flag a command

## 증상
When Claude Code detects potentially risky command syntax, it prompts the user for approval rather than auto-denying. For example:

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
PreToolUse hooks with `exit 2` can auto-block specific patterns. But this requires the user to reimplement detection that CC already has built in.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/28993
