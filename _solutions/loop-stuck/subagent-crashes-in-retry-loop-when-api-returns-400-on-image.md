---
layout: solution
title: "Subagent crashes in retry loop when API returns 400 on image processing"
category: loop-stuck
source: https://github.com/anthropics/claude-code/issues/37062
---

# Subagent crashes in retry loop when API returns 400 on image processing

## 증상
When a subagent attempts to read a PNG image file and sends it to the API, the API returns a 400 error (`Could not process image`). Instead of gracefully handling the error and moving on, the agent gets stuck in an infinite retry loop.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
1. 최대 재시도 제한: 동일 작업 3-5회 제한
2. 에러 패턴 감지: 같은 에러 반복 시 다른 접근법 전환
3. 타임아웃: 단일 작업 시간 제한 설정
4. 상태 체크포인트: 진행상황 기록으로 반복 방지
5. 에스컬레이션: 실패 시 사람에게 보고

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37062
