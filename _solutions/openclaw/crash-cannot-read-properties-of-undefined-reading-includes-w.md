---
layout: solution
title: "Crash: Cannot read properties of undefined (reading 'includes') when streaming model sends content:null during reasoning"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50780
---

# Crash: Cannot read properties of undefined (reading 'includes') when streaming model sends content:null during reasoning

## 증상
OpenClaw crashes with `Cannot read properties of undefined (reading 'includes')` when the upstream LLM provider sends streaming chunks with `"content": null` during the reasoning/thinking phase.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Remove models that send `content: null` (GLM-5, Qwen3.5-Plus, MiniMax-M2.5) from routing combos until the null-check is added.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50780
