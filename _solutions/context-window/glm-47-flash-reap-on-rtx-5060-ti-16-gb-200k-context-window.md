---
layout: solution
title: "GLM-4.7-Flash-REAP on RTX 5060 Ti 16 GB - 200k context window!"
category: context-window
source: Reddit r/ClaudeAI https://reddit.com/r/LocalLLaMA/comments/1qlanzn/glm47flashrea
---

# GLM-4.7-Flash-REAP on RTX 5060 Ti 16 GB - 200k context window!

## 증상
TL;DR: Here's my latest local coding setup, the params are mostly based on [Unsloth's recommendation for tool calling](https://unsloth.ai/docs/models/glm-4.7-flash#tool-calling-with-glm-4.7-flash)

- Model: [unsloth/GLM-4.7-Flash-REAP-23B-A3B-UD-Q3_K_XL](https://huggingface.co/unsloth/GLM-4.7-Flash-REAP-23B-A3B-GGUF)
- Repeat penalty: disabled
- Temperature: 0.7
- Top P: 1
- Min P: 0.01
- Standard

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
1. 대화 분할: 긴 작업은 여러 세션으로 분리
2. 요약 활용: 이전 대화를 구조화된 요약으로 대체
3. 선택적 컨텍스트: 관련 정보만 포함, 전체 파일 붙여넣기 금지
4. 주기적 리프레시: 20턴마다 컨텍스트 정리
5. 핵심 정보는 프롬프트 시작/끝에 배치

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/LocalLLaMA/comments/1qlanzn/glm47flashreap_on_rtx_5060_ti_16_gb_200k_context/
