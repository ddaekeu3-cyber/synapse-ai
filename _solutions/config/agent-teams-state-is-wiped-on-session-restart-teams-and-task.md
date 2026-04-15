---
layout: solution
title: "Agent Teams state is wiped on session restart — teams and tasks do not persist"
category: config
source: https://github.com/anthropics/claude-code/issues/33764
description: "Agent Teams state stored at and does not persist across sessions. When a Claude Code session exits (including crashes), the directories are emptied on"
---

# Agent Teams state is wiped on session restart — teams and tasks do not persist

## 증상
Agent Teams state stored at `~/.claude/teams/` and `~/.claude/tasks/` does not persist across sessions. When a Claude Code session exits (including crashes), the directories are emptied on next session startup, destroying all team configuration, task lists, and coordination state.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Maintain a separate team manifest and task snapshot in the project directory (outside `~/.claude/`) and manually recreate the team on each new session. This is fragile and defeats the purpose of the built-in team infrastructure.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33764
