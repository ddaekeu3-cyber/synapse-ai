---
layout: solution
title: "ACP persistent session for opencode dies immediately after spawn with queue owner unavailable, while acpx opencode exec/prompt works"
category: openclaw
---

# ACP persistent session for opencode dies immediately after spawn with queue owner unavailable, while acpx opencode exec/prompt works

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```text
summary: "queue owner unavailable"

This only seems to affect the OpenClaw-managed ACP persistent runtime path.

The following all work correctly on the same machine:

acpx opencode exec
manua

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53415 참조.

## 해결법
issue, and not a normal channel routing issue. The failure appears to be in the OpenClaw-managed ACP persistent runtime ownership / queue-owner path.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53415
