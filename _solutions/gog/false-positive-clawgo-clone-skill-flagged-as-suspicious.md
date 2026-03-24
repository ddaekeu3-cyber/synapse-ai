---
layout: solution
title: "False positive: clawgo-clone skill flagged as suspicious"
category: gog
---

# False positive: clawgo-clone skill flagged as suspicious

## 증상
My skill `clawgo-clone` (by @chenjunyeee ) was flagged as suspicious on upload.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #1220 참조.

## 해결법
ed workspace directory** and **temp paths** for zip extraction and timestamped backups, in the context of applying a user-chosen archive.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1220
