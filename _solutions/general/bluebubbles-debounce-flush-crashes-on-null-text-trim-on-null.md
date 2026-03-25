---
layout: solution
title: "BlueBubbles: debounce flush crashes on null text (.trim() on null)"
category: general
source: https://github.com/openclaw/openclaw/issues/35777
---

# BlueBubbles: debounce flush crashes on null text (.trim() on null)

## 증상
When a BlueBubbles webhook delivers a message with `text: null` (as opposed to `text: ""`), the debounce queue flush handler calls `.trim()` on the null value, causing a `TypeError` crash. This kills the entire flush batch, meaning **all queued messages in that debounce window are lost**.

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
https://github.com/openclaw/openclaw/issues/35777
