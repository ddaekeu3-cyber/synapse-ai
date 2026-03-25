---
layout: solution
title: "Session idle timeout disconnects during long-running background tasks"
category: performance
source: https://github.com/anthropics/claude-code/issues/32050
---

# Session idle timeout disconnects during long-running background tasks

## 증상
Claude Code sessions disconnect after a period of idle input, even when background tasks are actively running. This forces users to babysit sessions during long-running workflows instead of kicking off work and walking away.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Currently the only workaround is to chain everything into a standalone shell script that runs outside Claude Code, which defeats the purpose of the orchestration capabilities.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32050
