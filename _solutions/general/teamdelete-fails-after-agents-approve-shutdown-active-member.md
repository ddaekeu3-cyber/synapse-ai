---
layout: solution
title: "TeamDelete fails after agents approve shutdown - 'active members' not cleared"
category: general
source: https://github.com/anthropics/claude-code/issues/38116
---

# TeamDelete fails after agents approve shutdown - 'active members' not cleared

## 증상
After agents approve a `shutdown_request`, `TeamDelete` still fails with `Cannot cleanup team with N active member(s)`. The agents have:

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Force-removing team directories: `rm -r ~/.claude/teams/{name} ~/.claude/tasks/{name}`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38116
