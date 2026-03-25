---
layout: solution
title: "Claude repeatedly declares fixes 'done' without end-to-end verification — same bugs resurface daily"
category: performance
source: https://github.com/anthropics/claude-code/issues/37818
---

# Claude repeatedly declares fixes 'done' without end-to-end verification — same bugs resurface daily

## 증상
I'm a solo developer running a trading system with ~155 LaunchAgents, ~15K tests, and real money at stake. I've been using Claude Code daily since late 2025. The same class of mistake keeps happening despite explicit instructions, CLAUDE.md rules, hooks, and memory files telling Claude not to do it.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
actually works in the real environment. The next day (or weeks later), I discover it was broken the whole time.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37818
