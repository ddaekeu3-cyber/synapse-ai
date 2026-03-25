---
layout: solution
title: "[FEATURE] Case Study: Governing stateless sessions at scale with CLAUDE.md + MEMORY.md"
category: concurrency
source: https://github.com/anthropics/claude-code/issues/29990
---

# [FEATURE] Case Study: Governing stateless sessions at scale with CLAUDE.md + MEMORY.md

## 증상
When you scale Claude Code beyond prototyping — 171K lines, 96 DB tables, multiple parallel sessions over months — session statelessness becomes the primary failure mode. This post describes a 3-layer governance pattern (CLAUDE.md + MEMORY.md + parallel coordination) that solved it.

## 원인
보고된 버그/문제. 카테고리: concurrency.

## 해결법
3-Layer Governance

### Layer 1: CLAUDE.md — Incident-Driven Constitution

Every rule traces back to an actual production failure. This is not a style guide.

```markdown

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/29990
