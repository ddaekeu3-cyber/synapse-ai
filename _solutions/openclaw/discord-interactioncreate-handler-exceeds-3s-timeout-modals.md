---
layout: solution
title: "Discord INTERACTION_CREATE handler exceeds 3s timeout — modals broken"
category: openclaw
---

# Discord INTERACTION_CREATE handler exceeds 3s timeout — modals broken

## 증상
Discord modal buttons (components v2 with `modal` field) consistently show "This button has expired" because the InteractionEventListener takes 10-27 seconds to respond. Discord requires interaction responses within 3 seconds.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #52579 참조.

## 해결법
For `type: 3` (MESSAGE_COMPONENT) interactions that trigger modals:
1. Send `interaction.deferReply()` or `interaction.deferUpdate()` immediately (within 3s)
2. Process the agent session async
3. Show the modal via follow-up

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52579
