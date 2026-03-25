---
layout: solution
title: "I built a tool to cut image token costs for GPT-4 by 80% — launching on PH"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/SideProject/comments/1mhl6j5/i_built_a_to
---

# I built a tool to cut image token costs for GPT-4 by 80% — launching on PH

## 증상
https://preview.redd.it/kvnhnkkfn1hf1.png?width=1536&amp;format=png&amp;auto=webp&amp;s=79ccaecb279a92b226c0176388b9fcea917d4de9

Hey Peeps!  
I’m Rob, a solo founder and dev based in the UK. Over the past few months, I’ve been building a cross-platform desktop app called **LLM Image Optimizer**, and it’s finally ready to launch, and I would love some feedback!

**What it does:**  
LLM Image Optim

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
Reddit r/ClaudeAI https://reddit.com/r/SideProject/comments/1mhl6j5/i_built_a_tool_to_cut_image_token_costs_for_gpt4/
