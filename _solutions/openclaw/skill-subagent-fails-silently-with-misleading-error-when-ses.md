---
layout: solution
title: "Skill subagent fails silently with misleading error when session model is Opus but skill frontmatter specifies Sonnet"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38348
---

# Skill subagent fails silently with misleading error when session model is Opus but skill frontmatter specifies Sonnet

## 증상
When running a Claude Code session with Opus (1M context) and invoking a skill that specifies `model: sonnet` in its frontmatter, the skill systematically fails. The same skill works correctly when the session model is also Sonnet.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Switch the session model to Sonnet before invoking the skill (`/model` → Sonnet). The skill then works correctly with the same `model: sonnet` frontmatter.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38348
