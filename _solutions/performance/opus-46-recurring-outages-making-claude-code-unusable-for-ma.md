---
layout: solution
title: "Opus 4.6 recurring outages making Claude Code unusable for Max subscribers (March 17-18)"
category: performance
source: https://github.com/anthropics/claude-code/issues/35981
---

# Opus 4.6 recurring outages making Claude Code unusable for Max subscribers (March 17-18)

## 증상
Claude Code has been effectively unusable since March 17, 2026 due to recurring Opus 4.6 server-side degradation. Sessions hang for 10-15+ minutes on simple prompts. This is not a local issue — your own status page confirms 4 separate Opus 4.6 incidents in the last 24 hours alone.

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
https://github.com/anthropics/claude-code/issues/35981
