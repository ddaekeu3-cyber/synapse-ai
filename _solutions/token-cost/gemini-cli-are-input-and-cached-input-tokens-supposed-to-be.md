---
layout: solution
title: "Gemini CLI: Are input and cached input tokens supposed to be the main cost of using it?"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/Bard/comments/1ltci95/gemini_cli_are_inpu
---

# Gemini CLI: Are input and cached input tokens supposed to be the main cost of using it?

## 증상
One day, about a 100 Million input tokens, and a unexpected bill... I guess I will refrain from letting the CLI near my API token... On the flip side, BRL got some valuation over the USD in the past month, so it is a little less expensive...

So, maybe Gemini CLI needs some improvements to reduce the current input token utilization... 😥🤷‍♀️

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
Reddit r/ClaudeAI https://reddit.com/r/Bard/comments/1ltci95/gemini_cli_are_input_and_cached_input_tokens/
