---
layout: solution
title: "iMessage self-chat creates message loop causing NO_REPLY behavior"
category: openclaw
---

# iMessage self-chat creates message loop causing NO_REPLY behavior

## 증상
Behavior bug (incorrect output/state without crash)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #53582 참조.

## 해결법
**: Use separate iMessage contact/number for agent instead of self-chat.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53582
