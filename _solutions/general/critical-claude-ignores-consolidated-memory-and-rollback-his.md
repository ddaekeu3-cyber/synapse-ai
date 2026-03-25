---
layout: solution
title: "Critical: Claude ignores consolidated memory and rollback history, reintroduces destructive code — 4th protocol violation"
category: general
source: https://github.com/anthropics/claude-code/issues/35161
---

# Critical: Claude ignores consolidated memory and rollback history, reintroduces destructive code — 4th protocol violation

## 증상
Claude Code repeatedly violates safety protocols despite having explicit rules, memory files, and error catalogs that should prevent these exact scenarios. The latest incident (4th violation) involved reintroducing a destructive algorithm (FSRS) that had been explicitly rolled back after causing a production site decline.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
" without investigating WHY they exist
2. Proposes deploying code that was explicitly rolled back
3. Does not cross-reference git history before proposing changes
4. Does not recognize patterns from its own error catalog

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/35161
