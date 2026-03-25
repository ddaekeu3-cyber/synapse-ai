---
layout: solution
title: "Skill loading evaluates file content against bash permission patterns"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37504
---

# Skill loading evaluates file content against bash permission patterns

## 증상
When using the `Skill` tool to load a skill (e.g., `/writing-bash`), the skill file content appears to be evaluated against bash command permission patterns. If the SKILL.md body contains characters like `!` inside backtick code spans, the permission check fails with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Rephrase the SKILL.md content to avoid `!` inside backtick code spans. For example:
```
breaking negation conditionals and not-equal comparisons
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37504
