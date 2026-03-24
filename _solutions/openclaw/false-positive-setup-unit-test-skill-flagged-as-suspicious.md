---
layout: solution
title: "[False Positive] setup-unit-test skill flagged as suspicious"
category: openclaw
---

# [False Positive] setup-unit-test skill flagged as suspicious

## 증상
- **Skill Name:** setup-unit-test



## 원인
원본 이슈에서 확인 필요. GitHub Issue #1036 참조.

## 해결법
and validation for `projectDir` to prevent path injection.
- **Environment Checks**: Added checks to ensure scripts only run within valid local Git repositories.
- **Security Documentation**: Added a "Security & Permissions" section in `SKILL.md` to explicitly declare all high-privilege operations and their purposes.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1036
