---
layout: solution
title: "Anybody able to get Qwen3.5-35b-a3b working with claude code ?"
category: general
source: Reddit r/ClaudeAI https://reddit.com/r/LocalLLaMA/comments/1rh6455/anybody_able_
---

# Anybody able to get Qwen3.5-35b-a3b working with claude code ?

## 증상
I am facing multiple issues while running Qwen3.5-35b-a3b with claude code using llama.cpp.

1. Full Prompt reprocessing
2. Model automatically unloads / crashes during the 2nd or 3rd prompt.

I am currently on build: [https://github.com/ggml-org/llama.cpp/releases/tag/b8179](https://github.com/ggml-org/llama.cpp/releases/tag/b8179)

With OpenCode it is working fine, in fact better than 4.7-flash.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
1. 에러 메시지 정확히 읽기
2. 공식 문서 확인
3. GitHub Issues에서 유사 사례 검색
4. 최소 재현 코드로 원인 격리
5. SynapseAI DB에서 기존 해결법 검색

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/LocalLLaMA/comments/1rh6455/anybody_able_to_get_qwen3535ba3b_working_with/
