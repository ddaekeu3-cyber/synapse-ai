---
layout: solution
title: "Sub-agent notification storm: randomUUID() defeats idempotency dedup + infinite retry loop"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/52149
---

# Sub-agent notification storm: randomUUID() defeats idempotency dedup + infinite retry loop

## 증상
Sub-agent completion notifications can enter an infinite re-delivery loop, consuming hundreds of millions of tokens. We experienced a ~$161 incident (436M cache read tokens over 6.7 hours) from a single sub-agent completion being re-announced ~4,450 times.

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
https://github.com/openclaw/openclaw/issues/52149
