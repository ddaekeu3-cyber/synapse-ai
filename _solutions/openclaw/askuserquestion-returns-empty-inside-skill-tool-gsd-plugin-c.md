---
layout: solution
title: "AskUserQuestion returns empty inside Skill tool (GSD plugin context)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/30523
---

# AskUserQuestion returns empty inside Skill tool (GSD plugin context)

## 증상
When `AskUserQuestion` is called inside a skill invoked via the `Skill` tool (specifically GSD `/gsd:update`), the tool returns immediately with an empty answer without showing the interactive prompt UI to the user.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
from #29547 may not have shipped in 2.1.63 yet.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/30523
