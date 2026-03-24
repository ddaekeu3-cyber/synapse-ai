---
layout: solution
title: "publish/sync fails for all skills: 'multiple paginated queries' Convex error"
category: openclaw
---

# publish/sync fails for all skills: "multiple paginated queries" Convex error

## 증상
All `clawhub publish` and `clawhub sync` operations fail with the following error for account `hubtiger123`:

에러 메시지:
```
Uncaught Error: This query or mutation function ran multiple paginated queries. Convex only supports a single paginated query in each function.
    at async syncSkillSearchDigestsForOwnerPublisher

## 원인
원본 이슈에서 확인 필요. GitHub Issue #1176 참조.

## 해결법
pending for an actively used skill (`qstar-video-ecom`) that cannot be shipped.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1176
