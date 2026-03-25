---
layout: solution
title: "Ollama cold-start timeout silently exfiltrates data via fallback chain"
category: performance
source: https://github.com/openclaw/openclaw/issues/52818
---

# Ollama cold-start timeout silently exfiltrates data via fallback chain

## 증상
When using local Ollama models, the first request after model load triggers a **cold-start** that takes 13–60+ seconds depending on model size. OpenClaw's default LLM request timeout fires before Ollama can respond, resulting in a **timeout-triggered fallback** (HTTP 408) to the next model in the fallback chain — typically a cloud provider. User data intended for local processing is silently route

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
1. 병목 식별: 프로파일링으로 가장 느린 부분 찾기
2. 캐싱: 반복 연산/API 호출 캐싱
3. 병렬 처리: 독립 작업 동시 실행
4. 타임아웃 설정: 무한 대기 방지
5. 리소스 모니터링: CPU, 메모리, 네트워크 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52818
