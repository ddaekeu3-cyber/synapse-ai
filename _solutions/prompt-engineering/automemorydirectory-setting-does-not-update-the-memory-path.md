---
layout: solution
title: "autoMemoryDirectory setting does not update the memory path in system prompt"
category: prompt-engineering
source: https://github.com/anthropics/claude-code/issues/36636
description: "When is set in project settings, the system prompt still instructs the model to use the default path (). This causes the model to read/write memory at the"
---

# autoMemoryDirectory setting does not update the memory path in system prompt

## 증상
When `autoMemoryDirectory` is set in project settings, the system prompt still instructs the model to use the default path (`~/.claude/projects/<encoded-path>/memory/`). This causes the model to read/write memory at the wrong location, ignoring the custom setting entirely.

## 원인
Prompt structure conflict or ambiguous instruction caused the model to misinterpret the intended task. 카테고리: prompt-engineering.

## 해결법
Add an explicit override in project `CLAUDE.md` to force the model to use the correct path, since CLAUDE.md instructions take priority over the system prompt.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36636
