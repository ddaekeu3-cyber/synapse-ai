---
layout: solution
title: "feat: extraWorkspaceFiles config array for plugin-contributed Project Context files"
category: config
source: https://github.com/openclaw/openclaw/issues/21198
---

# feat: extraWorkspaceFiles config array for plugin-contributed Project Context files

## 증상
`loadWorkspaceBootstrapFiles` uses a hardcoded file list (AGENTS.md, SOUL.md, TOOLS.md, IDENTITY.md, USER.md, HEARTBEAT.md, BOOTSTRAP.md, MEMORY.md). There is no way for a plugin to add its own file to that list.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
`prependContext` from `before_agent_start` - works but ends up in the user message turn, not in the system prompt's Project Context section.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/21198
