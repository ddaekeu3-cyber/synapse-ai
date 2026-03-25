---
layout: solution
title: "[Feature]: WhatsApp Group message envelope missing participant/sender JID ⁠"
category: general
source: https://github.com/openclaw/openclaw/issues/39926
---

# [Feature]: WhatsApp Group message envelope missing participant/sender JID ⁠

## 증상
When a message is received in a WhatsApp group, the inbound envelope exposed to the agent only contains the group JID in the ⁠ from ⁠ field. The individual sender's participant JID is stripped before routing, making it impossible for the agent to identify which group member sent a given message.

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
https://github.com/openclaw/openclaw/issues/39926
