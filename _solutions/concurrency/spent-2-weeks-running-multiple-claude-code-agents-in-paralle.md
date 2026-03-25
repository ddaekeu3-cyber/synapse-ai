---
layout: solution
title: "Spent 2 weeks running multiple claude code agents in parallel with gastown. here's the honest take"
category: concurrency
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1qur3qq/spent_2_weeks
---

# Spent 2 weeks running multiple claude code agents in parallel with gastown. here's the honest take

## 증상
Steve Yegge dropped gas town on jan 1 - basically lets you run multiple claude code sessions coordinated through git worktrees. his first rule was "don't use this in its first weeks"

i work on 3 projects solo and the idea of parallel agents shipping while i context switch was too good. lasted about a day before installing it.lol

the good: beads (his git-backed task tracker) is genuinely great. t

## 원인
보고된 버그/문제. 카테고리: concurrency.

## 해결법
1. 락 사용: 공유 리소스에 적절한 락/뮤텍스
2. 원자적 연산: 경쟁 조건 방지
3. 큐 기반 처리: 메시지 큐로 통신
4. 타임아웃: 락 대기에 타임아웃 설정
5. 스트레스 테스트: 동시성 버그 발견

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1qur3qq/spent_2_weeks_running_multiple_claude_code_agents/
