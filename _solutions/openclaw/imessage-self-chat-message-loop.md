---
layout: solution
title: "iMessage self-chat creates message loop causing NO_REPLY"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53582
---

# iMessage self-chat creates message loop causing NO_REPLY

## 증상
iMessage channel enters loop when agent messages itself. Each reply triggers a new incoming message, creating rapid-fire loop. Eventually causes NO_REPLY state.

## 원인
Self-sent messages in iMessage are received as incoming messages. No self-message detection filter exists.

## 해결법
### iMessage 자체 루프 해결
1. `ignoreSelfMessages: true` 설정 추가
2. 메시지 발신자가 봇 자신인지 확인하는 필터 추가
3. 최소 응답 간격: 같은 대화에 2초 이내 재응답 차단
4. iMessage 대신 다른 채널 사용 검토 (Telegram, Discord 등)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53582
