---
layout: solution
title: "I tested GPT-5.1 Codex against Sonnet 4.5, and it's about time Anthropic bros take pricing seriously."
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1oy36ag/i_tested_gpt51_
---

# I tested GPT-5.1 Codex against Sonnet 4.5, and it's about time Anthropic bros take pricing seriously.

## 증상
I've used Claude Sonnets the most among LLMs, for the simple reason that they are so good at prompt-following and an absolute beast at tool execution. That also partly explains the maximum Anthropic revenue from APIs (code agents to be precise). They have an insane first-mover advantage, and developers love to die for. 

But GPT 5.1 codex has been insanely good. One of the first things I do when a

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1oy36ag/i_tested_gpt51_codex_against_sonnet_45_and_its/
