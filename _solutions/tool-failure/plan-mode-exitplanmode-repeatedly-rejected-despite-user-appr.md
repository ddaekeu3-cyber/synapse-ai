---
layout: solution
title: "Plan mode ExitPlanMode repeatedly rejected despite user approval"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/33870
---

# Plan mode ExitPlanMode repeatedly rejected despite user approval

## 증상
When in plan mode, calling `ExitPlanMode` was rejected 4+ times in a row despite the user clicking "approve" / "accept" each time. The system returned a rejection error as if the user had denied the tool call.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Making an edit to the plan file eventually triggered an automatic exit from plan mode, bypassing the `ExitPlanMode` tool entirely.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33870
