---
layout: solution
title: "GPT 5 Nano's Tokenizer is Broken (and FAR more expensive than 4.1's)"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/OpenAI/comments/1mme2hk/gpt_5_nanos_token
---

# GPT 5 Nano's Tokenizer is Broken (and FAR more expensive than 4.1's)

## 증상
Check out this reprex (reproducible example) of a head-to-head comparison of 4.1's cheapest (nano is 10/40 cents per million tokens in/out) model version to 5's (nano is 5/40) in Openai's API Playground.

For some reason, the same simple prompt yields the same output text ("Hi!"), but 5 nano counts that text as 200 tokens, instead of 4.1 nano's 3 tokens. There's No WAY that should be 200 tokens! G

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
Reddit r/ClaudeAI https://reddit.com/r/OpenAI/comments/1mme2hk/gpt_5_nanos_tokenizer_is_broken_and_far_more/
