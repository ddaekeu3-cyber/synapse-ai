---
layout: solution
title: "Feature Request: External Memory Provider API for Zero-Downtime Context Compaction"
category: context-window
source: https://github.com/openclaw/openclaw/issues/49233
---

# Feature Request: External Memory Provider API for Zero-Downtime Context Compaction

## 증상
We propose adding an **External Memory Provider API** to OpenClaw that enables zero-downtime context compaction by delegating memory management to an external system. This eliminates the current 30-60 second agent blackout during compaction — reducing it to **<100ms with zero perceived downtime**.

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
https://github.com/openclaw/openclaw/issues/49233
