---
layout: solution
title: "Skill tool $ARGUMENTS variable not populated when invoking skills with command blocks"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/18044
---

# Skill tool $ARGUMENTS variable not populated when invoking skills with command blocks

## 증상
When using the Skill tool to invoke a plugin skill that contains a command block with `$ARGUMENTS`, the `$ARGUMENTS` variable is not being populated with the args passed to the Skill tool.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Invoke the setup script directly via Bash instead of using the Skill tool:
```bash
~/.claude/plugins/cache/claude-plugins-official/ralph-loop/f70b65538da0/scripts/setup-ralph-loop.sh "Your prompt" --max-iterations 10
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/18044
