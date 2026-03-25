---
layout: solution
title: "Allow tools/skills to programmatically clear context and inject a continuation prompt"
category: context-window
source: https://github.com/anthropics/claude-code/issues/35150
---

# Allow tools/skills to programmatically clear context and inject a continuation prompt

## 증상
When working on long, multi-step tasks, the context window fills up and performance degrades. The natural response is to clear and continue, but this destroys all accumulated context — decisions made, files identified, progress tracked, corrections given.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
is for the skill to save a summary to a file, then the user manually runs `/clear`, then manually types "read the file and continue." This defeats the purpose of having a skill.

Related: #32861

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/35150
