---
layout: solution
title: "Scheduled tasks not appearing in 'Scheduled' sidebar section (desktop app)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37755
---

# Scheduled tasks not appearing in "Scheduled" sidebar section (desktop app)

## 증상
Scheduled tasks created via the `mcp__scheduled-tasks` MCP tool are stored correctly on disk and execute on schedule, but do not appear in the desktop app's "Scheduled" sidebar section.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Tasks function correctly despite not appearing in the UI. A custom slash command (`/scheduled-tasks-dashboard`) can be used as an alternative management interface.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37755
