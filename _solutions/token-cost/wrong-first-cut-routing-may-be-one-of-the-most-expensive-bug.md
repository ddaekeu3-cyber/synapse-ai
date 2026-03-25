---
layout: solution
title: "wrong first-cut routing may be one of the most expensive bugs in agent workflows"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/AutoGPT/comments/1rxqao5/wrong_firstcut_r
---

# wrong first-cut routing may be one of the most expensive bugs in agent workflows

## 증상
If you build with AutoGPT-style workflows a lot, you have probably seen this pattern already:

the model is often not completely useless. it is just wrong on the first cut.

it sees one local symptom, proposes a plausible action, and then the whole workflow starts drifting:

* wrong routing path
* wrong tool path
* repeated trial and error
* patch on top of patch
* extra side effects
* more system

## 원인
보고된 버그/문제. 카테고리: token-cost.

## 해결법
1. 모델 선택 최적화: 단순 작업은 Haiku, 복잡한 작업만 Opus 사용
2. 프롬프트 캐싱 활성화: 반복 시스템 프롬프트 캐싱으로 90% 절감
3. 컨텍스트 최소화: 필요한 정보만 포함
4. 에러 루프 방지: 3회 실패 시 다른 접근법으로 전환
5. 토큰 사용량 모니터링 대시보드 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/AutoGPT/comments/1rxqao5/wrong_firstcut_routing_may_be_one_of_the_most/
