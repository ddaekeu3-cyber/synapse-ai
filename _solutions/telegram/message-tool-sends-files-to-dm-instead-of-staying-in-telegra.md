---
layout: solution
title: "message tool sends files to DM instead of staying in Telegram topic"
category: telegram
---

# message tool sends files to DM instead of staying in Telegram topic

## 증상
Behavior bug (incorrect output/state without crash)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #52286 참조.

## 해결법
Extend OpenClaw's reply mechanism to support file/media attachments transparently, so the AI can reply with a file the same way it replies with text — without explicitly calling the message tool.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52286
