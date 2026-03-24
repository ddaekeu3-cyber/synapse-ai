---
layout: solution
title: "exec approval followup fails with 'Channel is required' in webchat-only setup"
category: gog
---

# exec approval followup fails with "Channel is required" in webchat-only setup

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```shell
[exec-followup] preparing agent call {
  hasDeliveryTarget: false,
  channel: 'webchat',
  to: undefined
}
[agent] incoming request { deliver: true }
[ws] res ✗ agent errorCode=INVALID_REQUES

## 원인
원본 이슈에서 확인 필요. GitHub Issue #51936 참조.

## 해결법
introduce const hasDeliveryTarget = Boolean(channel && to) in sendExecApprovalFollowup() and gate deliver and bestEffortDeliver on this flag. Webchat-only sessions keep the followup in session context without requiring an external channel. External channel flows (Discord, Telegram, Slack) are unaffected when channel and to are both present.
This issue is independent from the plain-HTTP approval-cl

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/51936
