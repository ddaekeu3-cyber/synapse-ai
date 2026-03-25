---
layout: solution
title: "NanoGPT subscription changes (requests -&gt; input tokens)"
category: performance
source: Reddit r/ClaudeAI https://reddit.com/r/SillyTavernAI/comments/1r5bycs/nanogpt_su
---

# NanoGPT subscription changes (requests -&gt; input tokens)

## 증상
Posting here what we've also posted in our Discord. Mods - hope this is okay, we know we have quite a lot of users from here so feel this is the best way to reach everyone.

**Subscription update** 

We've been struggling a bit with the subscription the last days/weeks for a few reasons:

1. Constant abuse. We've talked time to time about this in the chat - having for example 17 accounts that depo

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
Reddit r/ClaudeAI https://reddit.com/r/SillyTavernAI/comments/1r5bycs/nanogpt_subscription_changes_requests_input_tokens/
