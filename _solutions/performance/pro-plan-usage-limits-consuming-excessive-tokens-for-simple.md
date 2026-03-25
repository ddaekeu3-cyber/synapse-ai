---
layout: solution
title: "Pro plan usage limits consuming excessive tokens for simple tasks"
category: performance
source: https://github.com/anthropics/claude-code/issues/18786
---

# Pro plan usage limits consuming excessive tokens for simple tasks

## 증상
Usage limits have taken a drastic turn making Claude Code unusable on the Pro plan at least. 9% used just for a handful of css changes, not complex and only targeting 1 component in react. Only able to do a few basic prompts now before my usage is already 100% used up. I've got my context very lean now so that shouldn't be an issue. Feel like the reporting is broken for usage now.

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
https://github.com/anthropics/claude-code/issues/18786
