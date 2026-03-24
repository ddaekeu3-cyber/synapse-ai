---
layout: solution
title: "Telegram approval buttons not appearing for elevated commands despite `inlineButtons: 'all'` config"
category: telegram
---

# Telegram approval buttons not appearing for elevated commands despite `inlineButtons: "all"` config

## 증상
When running elevated commands (`elevated: true`), Telegram does not show the approval request message but **also not the "Approve" button**. The approval times out after several minutes.

에러 메시지:
```json
{
  "tools": {
    "elevated": {
      "enabled": true,
      "allowFrom": { "telegram": [7197206006] }
    }
  },
  "channels": {
    "telegram": {
      "enabled": true,
      "dmPolicy": "p

## 원인
원본 이슈에서 확인 필요. GitHub Issue #23856 참조.

## 해결법
File-Based Command Queue

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/23856
