---
layout: solution
title: "My lean Kilo Code override prompt (cuts token waste on expensive models)"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/kilocode/comments/1p2ik2a/my_lean_kilo_co
---

# My lean Kilo Code override prompt (cuts token waste on expensive models)

## 증상
Kilo Code is solid, but its default prompt is very large and drives up API costs especially with expensive models like Claude 4.5 Sonnet. I refactored the system prompt to 1/3 the size of the original; you should see a noticeable reduction in token usage per task.

I stay in Debug mode for everything, but the prompt should be transferable to all modes. Two habits help: (1) keep an LLM scratchpad o

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
Reddit r/ClaudeAI https://reddit.com/r/kilocode/comments/1p2ik2a/my_lean_kilo_code_override_prompt_cuts_token/
