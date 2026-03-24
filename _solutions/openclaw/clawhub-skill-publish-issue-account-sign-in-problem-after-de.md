---
layout: solution
title: "ClawHub skill publish issue + account sign-in problem after deleting account"
category: openclaw
---

# ClawHub skill publish issue + account sign-in problem after deleting account

## 증상
I’m facing two issues on ClawHub  related to publishing skills and authentication.

에러 메시지:
`
- I completed the license section and changelog.
- When clicking **Publish skill**, I received this error:

`

## 원인
원본 이슈에서 확인 필요. GitHub Issue #1046 참조.

## 해결법
the problem.

However:
- GitHub auth seems to complete
- But I am not actually logged into ClawHub
- The page still shows: `Sign in to upload a skill`
- It looks like the session/account is not being recreated correctly after deletion

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1046
