---
layout: solution
title: "Remote-control: Sending image from mobile crashes session with API 400 - missing media_type field"
category: general
source: https://github.com/anthropics/claude-code/issues/33852
---

# Remote-control: Sending image from mobile crashes session with API 400 - missing media_type field

## 증상
When sending an image via `/remote-control` from a mobile device (Claude.ai mobile app), the image is transmitted without the required `media_type` field in the base64 source payload. This causes a 400 API error that **poisons the conversation context**, making every subsequent message in the session also fail with the same error — effectively crashing the remote-control session entirely.

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
https://github.com/anthropics/claude-code/issues/33852
