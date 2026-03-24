---
layout: solution
title: "Cron 'New Job' form misaligns the Description field after interacting with Agent ID"
category: openclaw
---

# Cron "New Job" form misaligns the Description field after interacting with Agent ID

## 증상
On the dashboard Cron "New Job" form, interacting with the `Agent ID` field causes the adjacent `Description` field to become visibly misaligned in the Basics section layout.

에러 메시지:
`.
No crash or gateway error log was required to reproduce the issue.

## Impact and severity
- Affected: Dashboard users creating or editing cron jobs
- Severity: Medium
- Frequency: Reproduced on ev

## 원인
원본 이슈에서 확인 필요. GitHub Issue #1112 참조.

## 해결법
ed in a private/demo workspace, so no current screenshot is attached.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1112
