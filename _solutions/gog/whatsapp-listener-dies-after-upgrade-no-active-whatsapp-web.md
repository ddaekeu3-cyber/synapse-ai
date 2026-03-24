---
layout: solution
title: "WhatsApp listener dies after upgrade — 'No active WhatsApp Web listener' on all outbound messages (resolved by reverting to v2026.03.11)"
category: gog
---

# WhatsApp listener dies after upgrade — "No active WhatsApp Web listener" on all outbound messages (resolved by reverting to v2026.03.11)

## 증상
Regression (worked before, now fails)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #52773 참조.

## 해결법
**
Manually reverting to **v2026.03.11 (29dc654)** fully resolved the issue. 
WhatsApp outbound restored immediately after rollback with no other changes.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52773
