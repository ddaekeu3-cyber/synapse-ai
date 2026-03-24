---
layout: solution
title: "False positive flag: ui-craft-pro still marked suspicious after wording and payload cleanup"
category: openclaw
---

# False positive flag: ui-craft-pro still marked suspicious after wording and payload cleanup

## 증상
I published a skill called `ui-craft-pro` to ClawHub, but it is still being flagged as **"suspicious patterns detected"** by ClawHub Security.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #1120 참조.

## 해결법
the actual trigger rather than guessing.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1120
