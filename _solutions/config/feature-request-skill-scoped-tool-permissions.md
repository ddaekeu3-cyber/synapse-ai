---
layout: solution
title: "Feature request: skill-scoped tool permissions"
category: config
source: https://github.com/anthropics/claude-code/issues/35099
---

# Feature request: skill-scoped tool permissions

## 증상
Skills (`.claude/skills/<name>/SKILL.md`) have no way to scope tool/bash permissions to themselves. All permissions in `settings.local.json` are project-wide — you either grant a permission globally or accept approval prompts every time the skill runs.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Add permissions to `.claude/settings.local.json` (project-wide) or accept approval prompts on every invocation.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/35099
