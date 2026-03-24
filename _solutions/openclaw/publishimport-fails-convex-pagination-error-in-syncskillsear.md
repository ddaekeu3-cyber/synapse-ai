---
layout: solution
title: "Publish/Import fails: Convex pagination error in syncSkillSearchDigestsForOwnerPublisherId"
category: openclaw
---

# Publish/Import fails: Convex pagination error in syncSkillSearchDigestsForOwnerPublisherId

## 증상
Attempting to publish v1.4.2 of battlecard-competitive-intelligence fails across all three methods (CLI, Upload, Import). Error: "This query or mutation function ran multiple paginated queries. Convex only supports a single paginated query in each function." Stack trace points to syncSkillSearchDige



## 원인
원본 이슈에서 확인 필요. GitHub Issue #1182 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1182
