---
layout: solution
title: "Race condition between prompts and heartbeats causes lost message"
category: concurrency
source: https://github.com/openclaw/openclaw/issues/49252
---

# Race condition between prompts and heartbeats causes lost message

## 증상
When a prompt from the user occurs at the same time as a heartbeat in the same channel, the prompt disappears from the UI and the heartbeat is not immediately shown until the user performs a page refresh. After the refresh, the prompt appears "lost" — it may eventually trigger after the heartbeat, but if the heartbeat takes a long time, the user is left believing their prompt was lost.

## 원인
보고된 버그/문제. 카테고리: concurrency.

## 해결법
1. 락 사용: 공유 리소스에 적절한 락/뮤텍스
2. 원자적 연산: 경쟁 조건 방지
3. 큐 기반 처리: 메시지 큐로 통신
4. 타임아웃: 락 대기에 타임아웃 설정
5. 스트레스 테스트: 동시성 버그 발견

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49252
