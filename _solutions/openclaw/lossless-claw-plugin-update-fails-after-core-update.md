---
layout: solution
title: "lossless-claw plugin update fails after core update"
category: openclaw
---

# lossless-claw plugin update fails after core update

## 증상
After updating OpenClaw core from 2026.3.22 to 2026.3.23-2, the lossless-claw plugin update fails with:

에러 메시지:
```
Failed to update lossless-claw: Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/home/linuxbrew/.linuxbrew/lib/node_modules/openclaw/dist/install.runtime-Deq6Beal.js' imported from /home/linuxbr

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53426 참조.

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
https://github.com/openclaw/openclaw/issues/53426
