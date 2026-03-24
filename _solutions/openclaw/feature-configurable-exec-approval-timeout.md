---
layout: solution
title: "[Feature]: Configurable Exec Approval timeout"
category: openclaw
---

# [Feature]: Configurable Exec Approval timeout

## 증상
Allow user to override default timeouts for Exec Approvals.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #51287 참조.

## 해결법
Expose the DEFAULT_APPROVAL_TIMEOUT_MS and DEVAULT_APPROVAL_REQUEST_TIMEOUT_MS settings (found in /usr/local/lib/node_modules/openclaw/dist/plugin-sdk/model-auth-CutSHvqz.js ) to allow changes, presumably in the openclaw.json file.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/51287
