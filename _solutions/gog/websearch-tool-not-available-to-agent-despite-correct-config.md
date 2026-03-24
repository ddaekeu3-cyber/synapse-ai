---
layout: solution
title: "web_search tool not available to agent despite correct configuration"
category: gog
---

# web_search tool not available to agent despite correct configuration

## 증상
Regression (worked before, now fails)

에러 메시지:
```json
{
  "tools": {
    "web": {
      "search": {
        "enabled": true,
        "provider": "brave"
      }
    }
  },
  "plugins": {
    "enabled": true,
    "allow": ["", "imessage", "bluebub

## 원인
원본 이슈에서 확인 필요. GitHub Issue #51937 참조.

## 해결법
Attempted

None found. Config appears correct per documentation at `/app/docs/brave-search.md`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/51937
