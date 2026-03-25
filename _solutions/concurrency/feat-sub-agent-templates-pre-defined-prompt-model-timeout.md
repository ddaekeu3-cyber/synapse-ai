---
layout: solution
title: "feat: sub-agent templates (pre-defined prompt + model + timeout)"
category: concurrency
source: https://github.com/openclaw/openclaw/issues/50768
---

# feat: sub-agent templates (pre-defined prompt + model + timeout)

## 증상
Orchestrator agents that spawn sub-agents with recurring configurations (same prompt, model, timeout) have no way to pre-define reusable sub-agent types. Today, the orchestrator must manually read prompt files, assemble task strings, and pass model/timeout params on every `sessions_spawn` call. This is repetitive, error-prone, and makes the orchestration logic harder to follow.

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
https://github.com/openclaw/openclaw/issues/50768
