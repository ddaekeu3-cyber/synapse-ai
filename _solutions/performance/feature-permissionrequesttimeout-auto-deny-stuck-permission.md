---
layout: solution
title: "[FEATURE] permissionRequestTimeout — auto-deny stuck permission prompts after configurable timeout"
category: performance
source: https://github.com/anthropics/claude-code/issues/37913
---

# [FEATURE] permissionRequestTimeout — auto-deny stuck permission prompts after configurable timeout

## 증상
When running Claude Code sessions autonomously (e.g., via `--resume` in background terminals), a permission prompt can block the session indefinitely if the operator doesn't notice it. I've had sessions stuck waiting for hours on a mundane permission approval because I didn't realize the terminal was blocked.

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
https://github.com/anthropics/claude-code/issues/37913
