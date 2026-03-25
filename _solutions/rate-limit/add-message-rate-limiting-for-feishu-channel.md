---
layout: solution
title: "Add Message Rate Limiting for Feishu Channel"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/14206
---

# Add Message Rate Limiting for Feishu Channel

## 증상
Feishu server will temporarily ban api requests if messages are sent with high frequency. It seems that openclaw    separates responses from llm into a sequence of shorter messages, and send them one by one, sometimes triggering Feishu server limit.

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
1. 지수 백오프: 1초→2초→4초→8초 재시도 간격
2. 지터 추가: 랜덤 지터로 thundering herd 방지
3. 캐싱: 동일 요청 결과 캐싱
4. Retry-After 헤더 준수
5. 배치 처리: 개별 요청을 배치로 묶기

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/14206
