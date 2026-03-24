---
layout: solution
title: "[Feature]: Add maxToolCallsPerTurn config to limit rapid-fire tool call bursts"
category: general
---

# [Feature]: Add maxToolCallsPerTurn config to limit rapid-fire tool call bursts

## 증상
Add maxToolCallsPerTurn config to limit rapid-fire tool call bursts



## 원인
원본 이슈에서 확인 필요. GitHub Issue #47175 참조.

## 해결법
Add a maxToolCallsPerTurn config option (under agents.defaults or agents.defaults.subagents) that caps how many consecutive tool calls an agent can make before it must produce a text response to the user.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: general
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/47175
