---
layout: solution
title: "WhatsApp group echo creates infinite loop with bot mention"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/53386
---

# WhatsApp group echo creates infinite loop with bot mention

## 증상
In WhatsApp group, bot sends reply → group echoes it back with fromMe flag → bot treats it as new mention → replies again → infinite loop.

## 원인
fromMe echo in WhatsApp groups is not filtered. When bot replies in a group, the message echoes back and implicit reply-to-bot mention triggers another response.

## 해결법
### WhatsApp 무한 루프 해결
1. `filterFromMe: true` 설정 추가 (WhatsApp 플러그인 설정)
2. 메시지 ID 기반 중복 감지: 최근 응답한 메시지 ID 캐싱
3. 최소 응답 간격 설정: 같은 그룹에 5초 이내 재응답 차단
4. 그룹 설정에서 멘션만 응답하도록 변경 (에코 무시)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53386
