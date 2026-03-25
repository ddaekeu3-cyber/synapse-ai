---
layout: solution
title: "[Feature]:  Pre hook for messages in whatsapp"
category: general
source: https://github.com/openclaw/openclaw/issues/15066
---

# [Feature]:  Pre hook for messages in whatsapp

## 증상
if someone tries to reply to AI in the group and shes set ot only respond when shes mentioned its quite annoying. She should have an option to respond if replied directly to her post. I treid to implement a pre hook to catch when the reply is aimed at her to decide if we should stick in the trigger word and let her talk. We can also use pre hook for dynamic alllow list or something. But there is n

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
https://github.com/openclaw/openclaw/issues/15066
