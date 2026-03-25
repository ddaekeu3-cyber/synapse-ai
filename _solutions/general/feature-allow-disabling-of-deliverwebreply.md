---
layout: solution
title: "[Feature]: Allow disabling of deliverWebReply"
category: general
source: https://github.com/openclaw/openclaw/issues/19751
---

# [Feature]: Allow disabling of deliverWebReply

## 증상
deliverWebReply automatically sends audio from tmp folder to whatsapp. If the audio is in .mp3 format you cannot listen to it in mobile whatsapp and an .ogg coversion is needed, but this causes two voice messages to appear in whatsapp, one is .mp3 sent by deliverWebReply and another is the ogg conversion done by the agent to support mobile whatsapp audio format.

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
https://github.com/openclaw/openclaw/issues/19751
